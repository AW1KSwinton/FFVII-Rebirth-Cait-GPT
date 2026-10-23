-- Read-only Queen's Blood telemetry probe.
--
-- This probe intentionally uses an allow-list of reflected reads.  It does
-- not press input, change the board, enumerate arbitrary objects, or infer a
-- move from pixels.  Unknown runtime layouts are reported as unavailable so
-- the Python harness fails closed until the live field names are reconciled.
-- An explicit expiring developer request can run card_enumerator's widget
-- population experiment; normal sampling never invokes population functions.

local ROOT = "C:\\CaitGPT\\Telemetry\\"
local refs = require("object_refs")
local SAMPLE_PATH = ROOT .. "state.sample"
local OUTPUT_PATH = ROOT .. "queen_blood.json"
local TEMP_PATH = ROOT .. "queen_blood.json.tmp"
local WORKER_DEBUG_PATH = ROOT .. "queen_blood_probe.worker"
local GAME_DEBUG_PATH = ROOT .. "queen_blood_probe.game"
-- Always-on, change-only record of why a tick did not publish. It is
-- rewritten only when the outcome changes, so steady state costs no I/O.
local STATUS_PATH = ROOT .. "queen_blood_probe.status"
-- Reflected object discovery is comparatively expensive on the UE4 game
-- thread.  The card state changes on player actions, not every render tick,
-- so sample at a bounded rate and skip unchanged exporter sequences below.
local SAMPLE_PERIOD_MS = 500
local MAX_OBJECTS = 128
local MAX_UI_OBJECTS = 16
-- Keep reflection and file I/O off the hot path.  The UI roots and match
-- manager are stable for the lifetime of a screen, while a card-game
-- transition can make repeated discovery calls stall the game thread.
local UI_LOOKUP_PERIOD_SECONDS = 1
local MANAGER_LOOKUP_PERIOD_SECONDS = 1
-- Temporary one-run diagnostics for the deck-screen discovery boundary.
-- Revert to false immediately after collecting the bounded candidate log.
local DEBUG_ENABLED = false
-- The EndBillboard inventory fallback is retained below as an explicit
-- diagnostic path, but it is not safe in the normal sampler: FindAllOf on
-- that class can block the UE4SS callback while the mulligan UI is changing.
local LIVE_BILLBOARD_FALLBACK_ENABLED = false

-- This is a compact, offline-built join of the authoritative
-- CardGameCardInfo table to the English Resident_TxtRes resource.  Runtime
-- telemetry only needs to look up the short Card_### key extracted from the
-- live card artwork path; it never walks the catalogue UObject or calls the
-- unsafe in-game text-resource API.
local card_catalogue = {}
do
    local ok, value = pcall(require, "card_catalogue")
    if ok and type(value) == "table" then card_catalogue = value end
end

local auxiliary_card_catalogue = type(card_catalogue.auxiliary_cards) == "table"
    and card_catalogue.auxiliary_cards or {}
-- Card art name -> card id, from CardGameCardInfo. Art names do not follow
-- card ids for tokens (Card_Token_003 is shared by three tokens) and spawned
-- cards such as Moogle Trio's Moogle Mage/Bard only have token art.
local image_card_index = type(card_catalogue.image_cards) == "table"
    and card_catalogue.image_cards or {}

local function catalogue_entry_for_identity(identity)
    if not identity then return nil end
    return card_catalogue[identity.asset_key]
        or card_catalogue[identity.card_id]
        or auxiliary_card_catalogue[identity.card_id]
end

local function effect_card_definitions(catalogue_entry)
    if type(catalogue_entry) ~= "table" then return nil end
    local parameters = catalogue_entry.effect_text_parameters
    if type(parameters) ~= "table" then return nil end
    local definitions = {}
    for _, card_id in ipairs(parameters) do
        local definition = auxiliary_card_catalogue[card_id]
        if type(definition) == "table" then
            definitions[#definitions + 1] = definition
        end
    end
    return #definitions > 0 and definitions or nil
end

local MANAGER_CLASSES = {
    "EndCardGame3DManager",
    "EndGame.EndCardGame3DManager",
    "/Script/EndGame.EndCardGame3DManager",
    "EndCardGame3DManager_C",
    "CardGame3DManager",
}
local BOARD_CLASSES = {"EndCardGameBoard", "EndCardGameBoardActor", "CardGameBoard"}
local CARD_CLASSES = {"EndCardGameCard", "CardGameCard"}
local HAND_CLASSES = {"EndCardGameHand", "CardGameHand"}
local MOVE_CLASSES = {"EndCardGameLegalMove", "CardGameLegalMove"}
-- Every entry is a full UObject-array scan when absent. "EndCardGameUI" and
-- "CardGameUI" were removed: neither class exists in the CXXHeaderDump, and
-- scanning for them every second starved the game thread during mulligan.
local UI_CLASSES = {
    "CardGame_Top_C",
    "CardGame_Test_C",
    -- CardGame_Top's extracted Blueprint references these concrete native /
    -- generated menu boundaries.  They are cheaper and more reliable screen
    -- presence signals when the top-level UserWidget is not in FindAllOf's
    -- live inventory during the dialogue-to-deck transition.
    "EndCardGameMenu",
    "EndCardGameConfirmWindow",
    "CardGame_ConfirmWindow_C",
    -- The generated roots are not consistently returned during the first
    -- deck-screen tick. These concrete children are stable live presence
    -- signals and avoid scanning the entire UserWidget inventory.
    "CardGame_Test_CardBackSmall_C",
    "CardGame_CardFrame_C",
    "CardGame_Card_Full_C",
    -- The pre-match deck-select screen is its own viewport root. The
    -- EndCardGameMenu found beside it is a level actor, which has no
    -- IsInViewport and so cannot prove the screen is up (live 2026-09-28:
    -- deck screen on display, no card UI accepted, deck_screen unpublished).
    "MainMenu_Card_TopNPC_C",
}
local UI_PATHS = {
    "/Game/Menu/MiniGame/CardGame/CardGame_Top.CardGame_Top_C",
    "/Game/Menu/MiniGame/CardGame/CardGame_Test.CardGame_Test_C",
}
local MANAGER_PATHS = {
    "/Game/Level/Game/Field/0000-COMON/CardGame/Layout/0000-COMON_CardGame_Gimmick.0000-COMON_CardGame_Gimmick:PersistentLevel.EndCardGame3DManager",
    "/Game/Level/Game/Field/0000-COMON/CardGame/Layout/0000-COMON_CardGame_Gimmick.0000-COMON_CardGame_Gimmick:PersistentLevel.EndCardGame3DManager_0",
    "/Game/Level/Game/Field/0000-COMON/CardGame/Layout/0000-COMON_CardGame_Gimmick.0000-COMON_CardGame_Gimmick:PersistentLevel.EndCardGame3DManager_1",
    "/Game/Level/Game/Field/0000-COMON/CardGame/Layout/0000-COMON_CardGame_Gimmick.0000-COMON_CardGame_Gimmick:PersistentLevel.EndCardGame3DManager_2",
    "/Game/Level/Game/Field/0000-COMON/CardGame/Layout/0000-COMON_CardGame_Gimmick.0000-COMON_CardGame_Gimmick:PersistentLevel.EndCardGame3DManager_3",
    "/Game/Level/Game/Field/0000-COMON/CardGame/Layout/0000-COMON_CardGame_Gimmick.0000-COMON_CardGame_Gimmick:PersistentLevel.EndCardGame3DManager_4",
}
local last_sample_sequence = nil
local last_manager_fallback_at = 0
local MANAGER_FALLBACK_PERIOD_SECONDS = 5
local last_ui_lookup_at = 0
local cached_ui_object = nil
local cached_ui_class = nil
local next_ui_class_index = 1
local last_manager_lookup_at = 0
local cached_manager = nil
local manager_lookup = {}
local last_card_data_object_debug_at = 0
local last_card_image_identity_debug_at = 0
-- Mid-match hand identities are re-read only when the hand changes (a card
-- drawn or played) or every hand_ids.refresh_seconds (5 s): live 2026-09-30
-- the once-a-second re-read cost 34 ms a call on the game thread. A pool
-- widget can be reused for a newly drawn card, so a card whose widget was not
-- in the hand when identities were last read publishes no identity until the
-- next read, rather than the previous card's.
-- (One table: the probe's main chunk is at Lua's 200-local limit.)
local hand_ids = {refresh_seconds = 5, seen = nil, read = nil, read_at = nil, missing = true}
local card_focus_hooks_registered = false
local card_focus_hook_ids = {}
local live_card_focus_slot = nil
local live_card_focus_source = nil
local card_widget_reference_cache = {}
-- The hand roots and the five CardFull widgets are stable for a screen, but
-- rediscovering them is a full UObject-array scan per FindAllOf. Reuse them
-- as paths, resolving fresh objects on the game thread on each read.
local HAND_SCAN_REFRESH_SECONDS = 10
local cached_hand_roots = {}
local cached_hand_roots_at = 0
local cached_full_widgets = nil
-- CardHandActors entries from the last manager sample. The manager itself is
-- rediscovered at most every MANAGER_FALLBACK_PERIOD_SECONDS, so without this
-- the published cursor could lag input by several seconds.
local cached_hand_actors = nil
local last_manager_identity = nil
local last_card_indices = {}
local manager_hand_seen = false
local live_card_identity_by_slot = {}
local live_card_probe = {
    root_count = 0,
    root_path_count = 0,
    full_candidate_count = 0,
    full_valid_count = 0,
    full_live_match_count = 0,
    full_slot_match_count = 0,
    visual_widget_count = 0,
    billboard_count = 0,
    image_path_count = 0,
    asset_path_count = 0,
    identity_count = 0,
    slot_diagnostics = {},
    root_path = nil,
    first_full_name = nil,
    first_live_full_name = nil,
}
-- The mulligan marker is not part of the card identity, and neither
-- CardGame_Card_Full_C nor its native UEndCardGameCardPane parent has a
-- reflected Boolean for it (CXXHeaderDump). The cooked widget animations are
-- the authoritative contract: Card_Mulligan keys Pnl_Mulligan.Visibility to
-- Visible, while Card_Normal and Card_Selected key it to Collapsed. The red
-- prohibition glyph (Img_Mulligan_Icon) and its backing plate
-- (Img_Mulligan_Base) are the panel's only two slots, so Slot.Parent of
-- either authored image is the panel itself. Resolve only that one panel per
-- live card and read only its Visibility enum.
local MULLIGAN_PANEL_NAME = "Pnl_Mulligan"
local MULLIGAN_PANEL_CHILD_FIELDS = {"Img_Mulligan_Icon", "Img_Mulligan_Base"}
-- ESlateVisibility: Visible=0, Collapsed=1, Hidden=2, HitTestInvisible=3,
-- SelfHitTestInvisible=4. The cooked animations only ever write Visible or
-- Collapsed. While cards are being dealt, before any animation has played,
-- the panel sits at its authored default (4) with no glyph drawn; live
-- validation saw all five read 4, so every other value stays unknown.
local MULLIGAN_PANEL_VISIBILITY = {[0] = true, [1] = false}
local MULLIGAN_PANEL_VISIBILITY_NAMES = {visible = 0, collapsed = 1}
-- A failed resolution is retried at most this often per card widget so a
-- missing panel cannot turn the sampler into a per-tick path search.
local MULLIGAN_PANEL_RETRY_SECONDS = 2
local function safe(default, callback)
    local ok, value = pcall(callback)
    if ok then return value end
    return default
end

local function valid(value)
    return safe(false, function() return value ~= nil and value:IsValid() end)
end

local function text(value)
    if value == nil then return nil end
    local converted = safe(nil, function() return value:ToString() end)
    if converted ~= nil then value = converted end
    local result = tostring(value)
    if result == "" or result == "nil" then return nil end
    return result
end

-- The packaged CardGame_Card_Full blueprint contains Txt_CardName, an
-- EndTextBlock whose authored Text value is the displayed card name. Map only
-- exact catalogue names back to their authoritative card identity; an
-- unrecognized/ambiguous string remains unavailable.
local function normalized_card_name(value)
    local result = text(value)
    if result == nil then return nil end
    result = result:gsub("%s+", " "):gsub("^%s+", ""):gsub("%s+$", "")
    if result == "" then return nil end
    return result:lower()
end

local card_catalogue_by_name = {}
for asset_key, entry in pairs(card_catalogue) do
    if type(asset_key) == "string"
        and asset_key:match("^Card_%d%d%d$")
        and type(entry) == "table" then
        local name = text(entry.name)
        if name ~= nil then
            local normalized = normalized_card_name(name)
            if card_catalogue_by_name[normalized] ~= nil then
                card_catalogue_by_name[normalized] = false
            else
                card_catalogue_by_name[normalized] = {asset_key = asset_key, entry = entry}
            end
        end
    end
end

local function card_identity_from_name(value)
    local name = text(value)
    local normalized = normalized_card_name(name)
    if normalized == nil then return nil end
    name = name:gsub("%s+", " "):gsub("^%s+", ""):gsub("%s+$", "")
    local match = card_catalogue_by_name[normalized]
    if type(match) ~= "table" then return nil end
    return {
        asset_key = match.asset_key,
        card_id = text(match.entry.card_id),
        image_id = "U_Com_Billboard_CardGame_Card_" .. match.asset_key:match("(%d%d%d)$"),
        card_name = name,
        identity_source = "CardGame_Card_Full_C.Txt_CardName.Text",
        name_source = "CardGame_Card_Full_C.Txt_CardName.Text->CardGameCardInfo.Name",
    }
end

-- UI object paths can outlive the screen that created them. Require a visible
-- widget and walk its parents, because a child may keep Visibility=Visible
-- while its owning card-game screen is collapsed. Unknown visibility is not
-- enough to treat an object as the current screen.
local function card_ui_widget_shown(widget)
    local seen = {}
    for _ = 1, 24 do
        if not valid(widget) then return false end
        local identity = text(safe(nil, function() return widget:GetFullName() end))
        if identity == nil or seen[identity] then return false end
        seen[identity] = true
        local visible = safe(nil, function() return widget:IsVisible() end)
        local opacity = safe(nil, function() return widget:GetRenderOpacity() end)
        if type(visible) ~= "boolean" or type(opacity) ~= "number"
            or opacity ~= opacity or math.abs(opacity) > 1.0e30 then
            return false
        end
        if not visible or opacity <= 0.05 then return false end
        local parent = safe(nil, function() return widget:GetParent() end)
        if not valid(parent) then return true end
        widget = parent
    end
    return false
end

-- A retained UserWidget can still report visible after it has been removed
-- from the active viewport. For tree children, follow UObject outers until a
-- card-game screen root proves that it is visible and in the viewport.
-- IsInViewport is only required from the owner; ordinary WidgetTree children
-- remain eligible.
local function card_ui_has_viewport_owner(object)
    local seen = {}
    for _ = 1, 24 do
        if not valid(object) then return false end
        local identity = text(safe(nil, function() return object:GetFullName() end))
        if identity == nil or seen[identity] then return false end
        seen[identity] = true
        local class_name = (identity:match("^([^%s]+)") or ""):lower()
        -- IsInViewport exists only on UserWidgets: called on every outer
        -- (WidgetTree, panels) it logged an error each second of a match.
        local function in_viewport()
            return safe(nil, function() return object:IsInViewport() end) == true
        end
        if class_name == "cardgame_top_c" or class_name == "cardgame_test_c"
            or class_name == "mainmenu_card_topnpc_c" then
            -- These are the two extracted top-level card-game screens. If one
            -- is detached, do not mistake a retained child for its live UI.
            return in_viewport() and card_ui_widget_shown(object)
        end
        if (class_name == "endcardgamemenu"
            or class_name == "endcardgameconfirmwindow"
            or class_name == "cardgame_confirmwindow_c") and in_viewport() then
            -- A confirmation/menu UserWidget can itself be a viewport root.
            -- If it is embedded or detached, continue to its owning screen.
            return card_ui_widget_shown(object)
        end
        local outer = safe(nil, function() return object:GetOuter() end)
        if not valid(outer) then return false end
        object = outer
    end
    return false
end

local function is_live_card_ui_instance(object)
    if not valid(object) then return false end
    local full_name = text(safe(nil, function() return object:GetFullName() end))
    if not full_name then return false end
    local lowered = full_name:lower()
    -- StaticFindObject(path-to-blueprint) resolves the generated class even
    -- when no card-game widget is on screen.  Reject class/default objects,
    -- then require live visibility before accepting a constructed instance.
    if lowered:find("blueprintgeneratedclass", 1, true)
        or lowered:find("widgetblueprintgeneratedclass", 1, true)
        or lowered:find("default__", 1, true) then
        return false
    end
    local card_ui_path = lowered:find("cardgame_top_c /", 1, true) ~= nil
        or lowered:find("cardgame_test_c /", 1, true) ~= nil
        or lowered:find("cardgame_mulliganmessage_c /", 1, true) ~= nil
        or lowered:find("endcardgamemenu /", 1, true) ~= nil
        or lowered:find("endcardgameconfirmwindow /", 1, true) ~= nil
        or lowered:find("cardgame_confirmwindow_c /", 1, true) ~= nil
        or lowered:find("mainmenu_card_topnpc_c /", 1, true) ~= nil
        -- UE4SS exposes constructed roots through WidgetTree children on some
        -- ticks. These children are still subject to their own and parent
        -- visibility checks below.
        or lowered:find("/cardgame_top.cardgame_top_c:widgettree.", 1, true) ~= nil
        or lowered:find("/cardgame_test.cardgame_test_c:widgettree.", 1, true) ~= nil
        -- Some native menu classes are reported as PersistentLevel objects.
        -- Their path alone is not proof that the screen is on display.
        or (lowered:find(":persistentlevel.", 1, true) ~= nil
            and (lowered:find("endcardgamemenu", 1, true) ~= nil
                or lowered:find("endcardgameconfirmwindow", 1, true) ~= nil))
    return card_ui_path and card_ui_has_viewport_owner(object)
        and card_ui_widget_shown(object) or false
end

-- Only constructed top/test card-game roots are valid card identity roots.
-- Confirmation/menu children can establish UI presence but must not trigger a
-- five-card scan or marker reflection during the Begin Match transition.
local function is_live_card_hand_root(object)
    if not valid(object) then return false end
    local full_name = text(safe(nil, function() return object:GetFullName() end))
    if not full_name then return false end
    local class_name = full_name:match("^([^%s]+)")
    if class_name == nil then return false end
    class_name = class_name:lower()
    if class_name ~= "cardgame_top_c" and class_name ~= "cardgame_test_c" then
        return false
    end
    return true
end

local function number(value)
    local result = tonumber(value)
    if result == nil or result ~= result or math.abs(result) > 1000000000 then return nil end
    return result
end

local function integer(value)
    local result = number(value)
    if result == nil then return nil end
    return math.floor(result)
end

-- CardGame_Test_C authors CardFull_01..15 as one pool of hand-card faces.
-- A hand actor's CardIndex names its pool widget (CardFull_{CardIndex+1});
-- at the opening deal CardIndex equals the hand position, but after a
-- mulligan or a play the pool slot and the displayed position diverge.
local CARD_WIDGET_POOL_SIZE = 15

local function card_hand_index_from_widget_slot(value)
    local slot = integer(value)
    if slot == nil then return nil end
    if slot >= 1 and slot <= CARD_WIDGET_POOL_SIZE then return slot - 1 end
    return nil
end

local function boolean(value)
    if type(value) == "boolean" then return value end
    if value == 0 or value == "0" or value == "false" then return false end
    if value == 1 or value == "1" or value == "true" then return true end
    return nil
end

local function json_escape(value)
    return '"' .. tostring(value or ""):gsub('[%z\1-\31\\"]', function(character)
        local escapes = {['\\']='\\\\', ['"']='\\"', ['\b']='\\b', ['\f']='\\f', ['\n']='\\n', ['\r']='\\r', ['\t']='\\t'}
        return escapes[character] or string.format('\\u%04x', string.byte(character))
    end) .. '"'
end

local function json_number(value)
    local result = number(value)
    return result and string.format("%.6f", result) or "null"
end

local function json(value)
    if value == nil then return "null" end
    if type(value) == "string" then return json_escape(value) end
    if type(value) == "boolean" then return value and "true" or "false" end
    if type(value) == "number" then return json_number(value) end
    if type(value) == "table" then
        local array = true
        local highest = 0
        for key, _ in pairs(value) do
            if type(key) ~= "number" then array = false; break end
            if key > highest then highest = key end
        end
        local parts = {}
        if array then
            for index = 1, highest do parts[#parts + 1] = json(value[index]) end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        for key, item in pairs(value) do
            parts[#parts + 1] = json_escape(key) .. ":" .. json(item)
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return json_escape(text(value) or "")
end

local function write_debug(path, label, detail)
    if not DEBUG_ENABLED then return end
    pcall(function()
        local file = io.open(path, "ab")
        if not file then return end
        file:write(os.date("!%Y-%m-%dT%H:%M:%SZ") .. "|" .. tostring(label) .. "|" .. tostring(detail or "") .. "\n")
        file:flush()
        file:close()
    end)
end

local function card_identity_from_asset_path(value)
    local asset_path = text(value)
    if not asset_path then return nil end
    -- "/Game/.../U_Com_Billboard_CardGame_Card_Token_035.U_Com_..._Token_035"
    local image_name = asset_path:match("([%w_]+)$")
    local indexed = image_name and image_card_index[image_name] or nil
    if type(indexed) == "string" then
        local number = indexed:match("^card_id_(%d%d%d)$")
        return {
            asset_key = number and ("Card_" .. number) or nil,
            card_id = indexed,
            image_id = image_name,
            asset_path = asset_path,
        }
    end
    local digits = asset_path:match("CardGame_Card_(%d%d%d)")
    if not digits then return nil end
    return {
        asset_key = "Card_" .. digits,
        card_id = "card_id_" .. digits,
        image_id = "U_Com_Billboard_CardGame_Card_" .. digits,
        asset_path = asset_path,
    }
end

local function opaque_token(value)
    if value == nil then return nil end
    local ok, result = pcall(function() return tostring(value) end)
    if not ok or result == nil or result == "" or result == "nil" then return nil end
    return result
end

-- The gamepad cursor is a Slate/UMG focus transition, not a reflected
-- Boolean on CardFull. Observe only the authored UWidget focus setters and
-- retain a slot when the focused object is one of the five live hand cards.
-- This is bounded event telemetry; it does not enumerate widgets or inspect a
-- WidgetTree after rendering.
local function register_card_focus_hooks()
    if card_focus_hooks_registered then return true end
    if type(RegisterHook) ~= "function" then return false end
    local functions = {
        "/Script/UMG.Widget:SetFocus",
        "/Script/UMG.Widget:SetUserFocus",
        "/Script/UMG.Widget:SetKeyboardFocus",
    }
    local registered = 0
    for _, function_path in ipairs(functions) do
        local ok, pre_id, post_id = pcall(function()
            return RegisterHook(function_path, function(widget, child)
                local function record_focus_candidate(candidate, source)
                    if candidate == nil then return end
                    local full_name = text(safe(nil, function() return candidate:GetFullName() end)) or ""
                    local raw_slot = full_name:match("[Cc]ard[Ff]ull_(%d+)")
                    local hand_index = card_hand_index_from_widget_slot(raw_slot)
                    if hand_index ~= nil and hand_index >= 0 and hand_index <= 4 then
                        live_card_focus_slot = hand_index
                        live_card_focus_source = "focus_hook:" .. source
                    end
                end
                record_focus_candidate(widget, function_path)
                if function_path:find("SetFocusToChild", 1, true) then
                    record_focus_candidate(child, function_path .. ":child")
                end
            end)
        end)
        if ok then
            registered = registered + 1
            card_focus_hook_ids[#card_focus_hook_ids + 1] = {pre_id, post_id}
        end
    end
    card_focus_hooks_registered = registered > 0
    return card_focus_hooks_registered
end

local function read_field(object, names, debug_label)
    if not valid(object) then return nil, nil end
    for _, name in ipairs(names) do
        if debug_label then write_debug(GAME_DEBUG_PATH, debug_label .. "_before_" .. name, "") end
        local value = safe(nil, function() return object[name] end)
        if debug_label then
            write_debug(
                GAME_DEBUG_PATH,
                debug_label .. "_after_" .. name,
                value == nil and "nil" or (text(value) or "<non_text_value>")
            )
        end
        if value ~= nil then return value, name end
    end
    return nil, nil
end

-- UObject:GetFullName() is "<Class> <path>"; StaticFindObject takes the path.
local function object_path(object)
    local full_name = text(safe(nil, function() return object:GetFullName() end))
    if full_name == nil then return nil end
    return full_name:match(" (/.*)$") or full_name
end

local function object_name(object)
    return text(safe(nil, function() return object:GetFName():ToString() end))
end

-- UE4SS answers a missing property or a null reference with an invalid
-- UObject rather than nil, so every candidate must be valid *and* carry the
-- authored panel name before it is trusted.
local function accept_mulligan_panel(candidate)
    if not valid(candidate) then return nil, "invalid" end
    local name = object_name(candidate)
    if name ~= MULLIGAN_PANEL_NAME then return nil, "name:" .. tostring(name) end
    return candidate, "ok"
end

-- Resolve Pnl_Mulligan for one known live card widget. Every route is a
-- single reflected read or exact-path lookup on this card; none enumerates
-- objects or walks a WidgetTree. The first accepted panel is cached for the
-- lifetime of the card widget; a miss is retried on a bounded period and
-- keeps its per-route outcome for the slot diagnostics.
local function resolve_mulligan_panel(card_widget, card_path)
    if not valid(card_widget) or type(card_path) ~= "string" or card_path == "" then
        return nil, nil, nil
    end
    local cache_key = MULLIGAN_PANEL_NAME .. "|" .. card_path
    local cached = card_widget_reference_cache[cache_key]
    if cached ~= nil then
        local panel = refs.resolve(cached.path)
        if panel ~= nil then
            return panel, cached.source, cached
        end
        if cached.failed_at ~= nil
            and os.time() - cached.failed_at < MULLIGAN_PANEL_RETRY_SECONDS then
            return nil, nil, cached
        end
    end

    local attempts = {}
    local function remember(panel, source)
        local entry = {source = source, path = refs.path(panel), attempts = attempts}
        card_widget_reference_cache[cache_key] = entry
        return panel, source, entry
    end

    -- 1. A direct Blueprint variable, if the cooked class exposes one.
    local panel, status = accept_mulligan_panel(
        safe(nil, function() return card_widget[MULLIGAN_PANEL_NAME] end)
    )
    attempts.property = status
    if panel ~= nil then return remember(panel, "CardFull.Pnl_Mulligan") end

    -- 2. The panel that owns the authored glyph/plate image slots.
    for _, field in ipairs(MULLIGAN_PANEL_CHILD_FIELDS) do
        local child = safe(nil, function() return card_widget[field] end)
        if not valid(child) then
            attempts[field] = "child_invalid"
        else
            local panel_slot = safe(nil, function() return child.Slot end)
            if not valid(panel_slot) then
                attempts[field] = "slot_invalid"
            else
                panel, status = accept_mulligan_panel(
                    safe(nil, function() return panel_slot.Parent end)
                )
                attempts[field] = status
                if panel ~= nil then
                    return remember(panel, "CardFull." .. field .. ".Slot.Parent")
                end
            end
        end
    end

    -- 3. The card's own live WidgetTree. A duplicated tree is not guaranteed
    -- to be named "WidgetTree", so use the object's reported path.
    local tree = safe(nil, function() return card_widget.WidgetTree end)
    local tree_path = valid(tree) and object_path(tree) or nil
    if tree_path ~= nil then
        panel, status = accept_mulligan_panel(safe(nil, function()
            return StaticFindObject(tree_path .. "." .. MULLIGAN_PANEL_NAME)
        end))
        attempts.widget_tree = status
        if panel ~= nil then return remember(panel, "CardFull.WidgetTree.Pnl_Mulligan") end
    else
        attempts.widget_tree = "tree_invalid"
    end

    -- 4. The authored nested path under the card's reported path.
    panel, status = accept_mulligan_panel(safe(nil, function()
        return StaticFindObject(card_path .. ".WidgetTree." .. MULLIGAN_PANEL_NAME)
    end))
    attempts.authored_path = status
    if panel ~= nil then return remember(panel, "StaticFindObject:CardFull.WidgetTree.Pnl_Mulligan") end

    local failed = {failed_at = os.time(), attempts = attempts, widget_tree_path = tree_path}
    card_widget_reference_cache[cache_key] = failed
    return nil, nil, failed
end

local function call_method(object, names)
    if not valid(object) then return nil, nil end
    for _, name in ipairs(names) do
        local value = safe(nil, function()
            local method = object[name]
            if type(method) == "function" then return method(object) end
            return nil
        end)
        if value ~= nil then return value, name end
    end
    return nil, nil
end

local function first_object(classes)
    for _, class_name in ipairs(classes) do
        local object = safe(nil, function() return FindFirstOf(class_name) end)
        if valid(object) then return object, class_name end
    end
    return nil, nil
end

local function visibility_token(widget)
    if not valid(widget) then return nil end
    local function primitive_token(value)
        local value_type = type(value)
        if value_type == "string" then return value end
        if value_type == "number" or value_type == "boolean" then
            return tostring(value)
        end
        -- A missing UMG child is surfaced by UE4SS as a nullptr userdata.
        -- Calling UObject:ToString() on that value logs a native error every
        -- tick, so visibility is read only from primitive enum values here.
        return nil
    end
    local token = primitive_token(safe(nil, function() return widget.Visibility end))
    if token ~= nil then return token end
    local value = select(1, read_field(widget, {"Visibility"}))
    token = primitive_token(value)
    if token ~= nil then return token end
    return nil
end

-- Map only the two animation-keyed values. Substring matching is not safe
-- here: "SelfHitTestInvisible" contains "visible".
local function mulligan_panel_marker(token)
    if token == nil then return nil end
    local numeric = tonumber(token)
    if numeric == nil then
        numeric = MULLIGAN_PANEL_VISIBILITY_NAMES[token:lower():match("([%a]+)$") or ""]
    end
    if numeric == nil then return nil end
    return MULLIGAN_PANEL_VISIBILITY[numeric]
end

-- Returns true/false only from a resolved Pnl_Mulligan with a decodable
-- Visibility. Anything else is nil (unknown), never false.
local function read_mulligan_marker(card_widget, card_path)
    local panel, source, entry = resolve_mulligan_panel(card_widget, card_path)
    local visuals = {
        available = panel ~= nil,
        panel_source = source,
        panel_path = entry and entry.path or nil,
        resolution = entry and entry.attempts or nil,
        widget_tree_path = entry and entry.widget_tree_path or nil,
    }
    if panel == nil then return nil, nil, visuals end
    local token = visibility_token(panel)
    local visible = mulligan_panel_marker(token)
    visuals.visibility = token
    visuals.visible = visible
    if visible == nil then return nil, nil, visuals end
    return visible, source .. ".Visibility", visuals
end

local scan_live_card_full_widgets

local function live_card_full_widgets(live_root_paths)
    local key = table.concat(live_root_paths, "|")
    local cached = cached_full_widgets
    if cached ~= nil and cached.key == key
        and os.time() - cached.at < HAND_SCAN_REFRESH_SECONDS then
        local all_valid = true
        local result = {}
        for slot = 1, CARD_WIDGET_POOL_SIZE do
            result[slot] = refs.resolve(cached.paths[slot])
            if result[slot] == nil then all_valid = false; break end
        end
        if all_valid then
            live_card_probe.full_widgets_cached = true
            return result, cached.names, cached.paths
        end
    end
    local result, names, paths = scan_live_card_full_widgets(live_root_paths)
    local complete = true
    for slot = 1, CARD_WIDGET_POOL_SIZE do
        if result[slot] == nil then complete = false; break end
    end
    -- Cache only a complete pool so a partial construction is rescanned.
    cached_full_widgets = complete
        and {key = key, at = os.time(), names = names, paths = paths}
        or nil
    return result, names, paths
end

scan_live_card_full_widgets = function(live_root_paths)
    local result = {}
    local widget_names = {}
    local widget_paths = {}
    local objects = safe(nil, function() return FindAllOf("CardGame_Card_Full_C") end)
    if type(objects) ~= "table" then return result, widget_names, widget_paths end
    -- The main-menu card screens keep their own CardGame_Card_Full_C
    -- instances alive, so the live pool can sit past the first few dozen.
    live_card_probe.full_candidate_count = math.min(#objects, 128)
    for index = 1, math.min(#objects, 128) do
        local object = objects[index]
        if valid(object) then
            live_card_probe.full_valid_count = live_card_probe.full_valid_count + 1
            local full_name = text(safe(nil, function() return object:GetFullName() end)) or ""
            if live_card_probe.first_full_name == nil and full_name ~= "" then
                live_card_probe.first_full_name = full_name
            end
            local lowered = full_name:lower()
            local belongs_to_live_root = false
            for _, root_path in ipairs(live_root_paths) do
                if lowered:find(root_path, 1, true) then
                    belongs_to_live_root = true
                    break
                end
            end
            if belongs_to_live_root then
                live_card_probe.full_live_match_count = live_card_probe.full_live_match_count + 1
                if live_card_probe.first_live_full_name == nil and full_name ~= "" then
                    live_card_probe.first_live_full_name = full_name
                end
                local raw_slot = lowered:match("cardfull_(%d+)")
                local pool_index = card_hand_index_from_widget_slot(raw_slot)
                local slot = pool_index ~= nil and pool_index + 1 or nil
                if slot ~= nil and result[slot] == nil then
                    live_card_probe.full_slot_match_count = live_card_probe.full_slot_match_count + 1
                    result[slot] = object
                    widget_names[slot] = "CardFull_" .. tostring(raw_slot)
                    widget_paths[slot] = full_name:match(" (/.*)$")
                end
            end
        end
    end
    return result, widget_names, widget_paths
end

local function first_ui_object(classes)
    local now = os.time()
    -- Resolve the cached path before checking visibility; never touch an old UObject.
    -- Drop hidden or unreadable objects before applying the lookup throttle.
    local cached = refs.resolve(cached_ui_object)
    if is_live_card_ui_instance(cached) then
        return cached, cached_ui_class
    end
    cached_ui_object = nil
    cached_ui_class = nil
    if now - last_ui_lookup_at < UI_LOOKUP_PERIOD_SECONDS then
        return nil, nil
    end
    last_ui_lookup_at = now
    cached_ui_object = nil
    cached_ui_class = nil
    -- The live menu instances have stable cooked object paths.  Resolve those
    -- first so a nested CardGame_* child cannot mask the widget root when the
    -- broad UserWidget inventory is ordered by construction time.
    for _, path in ipairs(UI_PATHS) do
        local object = safe(nil, function() return StaticFindObject(path) end)
        if valid(object) then
            write_debug(
                GAME_DEBUG_PATH,
                "ui_exact_name",
                text(safe(nil, function() return object:GetFullName() end)) or ""
            )
        end
        if is_live_card_ui_instance(object) then
            write_debug(GAME_DEBUG_PATH, "ui_exact_candidate", path)
            cached_ui_object, cached_ui_class = refs.path(object), "StaticFindObject:" .. path
            return object, cached_ui_class
        end
    end
    -- Blueprint widget instances are not reliably returned by FindFirstOf in
    -- this title.  Keep the broader lookup limited to the two exact card UI
    -- classes and a small instance bound; ordinary gameplay never reaches
    -- their field reads unless one is actually present.
    -- Each absent class causes a full UObject-array scan. Spread discovery
    -- across ticks so ordinary exploration cannot pay eight scans at once.
    for _ = 1, math.min(1, #classes) do
        local class_name = classes[next_ui_class_index]
        next_ui_class_index = next_ui_class_index % #classes + 1
        local objects = safe(nil, function() return FindAllOf(class_name) end)
        if type(objects) == "table" then
            for index = 1, math.min(#objects, MAX_UI_OBJECTS) do
                local object = objects[index]
                if valid(object) then
                    write_debug(
                        GAME_DEBUG_PATH,
                        "ui_class_candidate",
                        class_name .. "|" .. (text(safe(nil, function() return object:GetFullName() end)) or "")
                    )
                end
                if is_live_card_ui_instance(object) then
                    cached_ui_object, cached_ui_class = refs.path(object), class_name
                    return object, cached_ui_class
                end
            end
        end
    end
    -- Do not fall back to FindAllOf("UserWidget") here. On this title that
    -- inventory contains roughly 1,800 objects and can delay publication
    -- beyond the harness action deadline. The concrete class probes above
    -- retry on the next bounded tick instead.
    return nil, nil
end

local function find_manager()
    local now = os.time()
    -- The manager exists only while a card game is loaded. Keep it while it
    -- is valid: dropping it every second but rediscovering at most every
    -- MANAGER_FALLBACK_PERIOD_SECONDS left the match phase, hand and cursor
    -- unpublished for most of each window.
    local cached = refs.resolve(cached_manager)
    manager_lookup = {source = "cached_path", cache_missed = cached_manager ~= nil and cached == nil}
    if cached ~= nil then return cached end
    local cache_missed = cached_manager ~= nil
    if now - last_manager_lookup_at < MANAGER_LOOKUP_PERIOD_SECONDS then
        manager_lookup.source = "lookup_cooldown"
        return nil
    end
    last_manager_lookup_at = now
    -- StaticFindObject and FindFirstOf can block the UE4SS game-thread
    -- callback exactly when the manager is being constructed for mulligan.
    -- The bounded class inventory is the discovery path confirmed by the
    -- live Queen's Blood scan, so use it directly and keep it throttled.
    -- A formerly valid path failing is different from an absent manager in
    -- the field. Reacquire once at the bounded one-second lookup rate rather
    -- than waiting out the five-second absent-manager discovery cooldown.
    -- Keep only its path until that attempt; no UObject is retained.
    if not cache_missed and now - last_manager_fallback_at < MANAGER_FALLBACK_PERIOD_SECONDS then
        manager_lookup.source = "discovery_cooldown"
        return nil
    end
    last_manager_fallback_at = now
    cached_manager = nil
    manager_lookup.source = "class_inventory"
    local objects = safe(nil, function() return FindAllOf("EndCardGame3DManager") end)
    if type(objects) == "table" then
        manager_lookup.candidate_count = math.min(#objects, MAX_OBJECTS)
        for index = 1, math.min(#objects, MAX_OBJECTS) do
            local manager = objects[index]
            local manager_name = valid(manager)
                and text(safe(nil, function() return manager:GetFullName() end)) or nil
            if manager_name ~= nil and not manager_name:find("Default__", 1, true) then
                cached_manager = refs.path(manager)
                return manager
            end
        end
    end
    return nil
end

local function all_objects(classes)
    local result = {}
    local seen = {}
    for _, class_name in ipairs(classes) do
        local objects = safe(nil, function() return FindAllOf(class_name) end)
        if type(objects) == "table" then
            for index = 1, math.min(#objects, MAX_OBJECTS) do
                local object = objects[index]
                if valid(object) then
                    local id = text(safe(nil, function() return object:GetFullName() end)) or tostring(object)
                    if not seen[id] then
                        seen[id] = true
                        result[#result + 1] = object
                        if #result >= MAX_OBJECTS then return result end
                    end
                end
            end
        end
    end
    return result
end

local function card_widget_hand_index(object, fallback_index)
    local full_name = text(safe(nil, function() return object:GetFullName() end)) or ""
    local slot = full_name:match("[Cc]ard[Ff]ull_(%d+)")
        or full_name:match("[Cc]ard[Ss]mall_(%d+)")
    local hand_index = card_hand_index_from_widget_slot(slot)
    if hand_index ~= nil then return hand_index end
    return fallback_index
end

local function read_sequence()
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then return nil end
    local line = file:read("*l")
    file:close()
    if not line then return nil end
    -- FF7RTelemetryExporter writes state.sample as a compact pipe-delimited
    -- record, not as JSON.  The first field is the authoritative sequence.
    return tonumber(line:match("^(%d+)|"))
end

local function position(value)
    if value == nil then return nil end
    local row = integer(safe(nil, function() return value.row or value.Row end))
    local column = integer(safe(nil, function() return value.column or value.Column or value.col end))
    if row == nil or column == nil or row < 0 or row > 2 or column < 0 or column > 4 then return nil end
    return {row = row, column = column}
end

-- The reconstructed card-game manager owns the actual drawn-hand actors.
-- Reading their authored CardIndex field is bounded and avoids reflected
-- property reads on generated UMG card widgets, which can block the UE4SS
-- game-thread callback during the mulligan animation.
local function reflected_array_items(value, maximum)
    local result = {}
    if type(value) == "table" then
        for index = 1, math.min(#value, maximum) do
            result[#result + 1] = {index = index - 1, object = value[index]}
        end
        return result
    end
    if value == nil then return result end
    local count = integer(safe(nil, function() return value:GetArrayNum() end))
    if count == nil or count <= 0 then return result end
    local zero_based = nil
    safe(nil, function()
        value:ForEach(function(index, item)
            if #result >= maximum then return end
            local object = safe(nil, function() return item:get() end) or item
            local raw_index = integer(index)
            if zero_based == nil and raw_index ~= nil then
                zero_based = raw_index == 0
            end
            local normalized_index = raw_index
            if normalized_index ~= nil and not zero_based then
                normalized_index = normalized_index - 1
            end
            result[#result + 1] = {index = normalized_index or (#result), object = object}
        end)
    end)
    return result
end

local function encode_mulligan_selection()
    -- Prefer a complete per-card marker set from the five exact CardFull
    -- instances.  A false value is meaningful: it proves that the producer
    -- can distinguish an unmarked card from an unavailable marker.
    local selected_indices = {}
    local source = nil
    local complete = true
    for slot = 0, 4 do
        local identity = live_card_identity_by_slot[slot]
        if identity == nil or identity.mulligan_selected == nil then
            complete = false
            break
        end
        if identity.mulligan_selected then
            selected_indices[#selected_indices + 1] = slot
        end
        source = source or identity.mulligan_selection_source
    end
    if complete and source ~= nil then
        return {
            observed = true,
            selected_indices = selected_indices,
            source = source,
        }
    end

    return {
        observed = false,
        selected_indices = {},
        source = nil,
    }
end

local function encode_manager_hand(manager)
    local raw_objects = select(1, read_field(manager, {"CardHandActors", "PlayerCardHandActors"}))
    local objects = reflected_array_items(raw_objects, CARD_WIDGET_POOL_SIZE)
    if #objects > 0 then
        cached_hand_actors = {}
        for _, entry in ipairs(objects) do
            cached_hand_actors[#cached_hand_actors + 1] = {index = entry.index, path = refs.path(entry.object)}
        end
    end
    write_debug(GAME_DEBUG_PATH, "manager_hand_array_type", type(raw_objects))
    write_debug(GAME_DEBUG_PATH, "manager_hand_array_count", #objects)
    local hand = {}
    local cursor_slot = nil
    local cursor_true_count = 0
    local cursor_observed = false
    local used_indices = {}
    local identity_missing = false
    for _, entry in ipairs(objects) do
        local object = entry.object
        if valid(object) then
            local card_index = integer(select(1, read_field(object, {"CardIndex"})))
            -- EndCardGame3DCardHand exposes this native/authored flag for the
            -- visible hand cursor.  Read exactly this field from the already
            -- bounded CardHandActors array; do not rediscover UMG focus or
            -- walk the generated widget tree.
            local cursor_visible = boolean(select(1, read_field(object, {"bCursorVisible"})))
            if cursor_visible ~= nil then
                cursor_observed = true
                if cursor_visible and entry.index >= 0 and entry.index < CARD_WIDGET_POOL_SIZE then
                    cursor_true_count = cursor_true_count + 1
                    cursor_slot = entry.index
                end
            end
            -- Unused hand actors report CardIndex -1. A used one names its
            -- CardFull pool widget, which is where the identity was read.
            if card_index ~= nil and card_index >= 0 then
                used_indices[#used_indices + 1] = card_index
                local identity = live_card_identity_by_slot[card_index]
                local read_for = hand_ids.read and ("," .. hand_ids.read .. ",") or ""
                if identity ~= nil and not read_for:find("," .. tostring(card_index) .. ",", 1, true) then
                    identity = nil  -- this widget was not in the hand when identities were read
                end
                if identity == nil then identity_missing = true end
                local catalogue_entry = catalogue_entry_for_identity(identity)
                local resolved_name = catalogue_entry and text(catalogue_entry.name) or nil
                write_debug(
                    GAME_DEBUG_PATH,
                    "hand_card_identity",
                    tostring(entry.index)
                        .. "|card_index=" .. tostring(card_index)
                        .. "|card_id=" .. ((identity and identity.card_id) or "<unknown>")
                        .. "|asset_key=" .. ((identity and identity.asset_key) or "<unknown>")
                        .. "|name=" .. (resolved_name or "<unknown>")
                )
                last_card_indices[entry.index] = card_index
                hand[#hand + 1] = {
                    -- hand_index is the displayed position, which is what the
                    -- cursor and navigation use; card_index is the pool widget.
                    hand_index = entry.index,
                    playable = boolean(select(1, read_field(object, {"bChooseableVisible"}))),
                    card_id = identity and identity.card_id or nil,
                    card_image_id = identity and identity.image_id or nil,
                    card_unique_id = catalogue_entry and integer(catalogue_entry.unique_id) or nil,
                    card_name_id = catalogue_entry and text(catalogue_entry.name_id) or nil,
                    -- These values come from the offline CardGameCardInfo /
                    -- CardGameSpecialParam join keyed by the live artwork
                    -- identity.  Never infer them from rendered glyphs.
                    cost = catalogue_entry and integer(catalogue_entry.cost) or nil,
                    power = catalogue_entry and integer(catalogue_entry.power) or nil,
                    influence = catalogue_entry and text(catalogue_entry.influence) or nil,
                    note_id = catalogue_entry and text(catalogue_entry.note_id) or nil,
                    effect_id = catalogue_entry and text(catalogue_entry.effect_id) or nil,
                    effect_text_id = catalogue_entry and text(catalogue_entry.effect_text_id) or nil,
                    effect_influence = catalogue_entry and text(catalogue_entry.effect_influence) or nil,
                    effect_constant = catalogue_entry and number(catalogue_entry.effect_constant) or nil,
                    effect_target = catalogue_entry and integer(catalogue_entry.effect_target) or nil,
                    effect_type = catalogue_entry and integer(catalogue_entry.effect_type) or nil,
                    effect_int_parameters = catalogue_entry and catalogue_entry.effect_int_parameters or nil,
                    effect_text_parameters = catalogue_entry and catalogue_entry.effect_text_parameters or nil,
                    effect_reference_text_id = catalogue_entry
                        and text(catalogue_entry.effect_reference_text_id) or nil,
                    effect_text = catalogue_entry and text(catalogue_entry.effect_text) or nil,
                    effect_text_source = catalogue_entry
                        and text(catalogue_entry.effect_text_source) or nil,
                    effect_type_name = catalogue_entry
                        and text(catalogue_entry.effect_type_name) or nil,
                    effect_target_name = catalogue_entry
                        and text(catalogue_entry.effect_target_name) or nil,
                    effect_text_label_references = catalogue_entry
                        and catalogue_entry.effect_text_label_references or nil,
                    effect_heading_types = catalogue_entry
                        and catalogue_entry.effect_heading_types or nil,
                    effect_parameter_references = catalogue_entry
                        and catalogue_entry.effect_parameter_references or nil,
                    effect_reference_counts = catalogue_entry
                        and catalogue_entry.effect_reference_counts or nil,
                    effect_filter_tags = catalogue_entry
                        and catalogue_entry.effect_filter_tags or nil,
                    effect_reference_count0 = catalogue_entry
                        and integer(catalogue_entry.effect_reference_count0) or nil,
                    effect_card_definitions = effect_card_definitions(catalogue_entry),
                    influence_square_count = catalogue_entry
                        and integer(catalogue_entry.influence_square_count) or nil,
                    influence_offsets = catalogue_entry and catalogue_entry.influence_offsets or nil,
                    effect_influence_square_count = catalogue_entry
                        and integer(catalogue_entry.effect_influence_square_count) or nil,
                    effect_influence_offsets = catalogue_entry
                        and catalogue_entry.effect_influence_offsets or nil,
                    card_index = card_index,
                    cursor_visible = cursor_visible,
                    mulligan_selected = identity and identity.mulligan_selected,
                    mulligan_selection_source = identity and identity.mulligan_selection_source or nil,
                    card_index_source = "hand_actor.CardIndex(CardFull_pool_index)",
                    card_identity_source = identity
                        and "CardFull_0N.Billboard_Thumbnail.ImagePath.AssetPathName" or nil,
                    name = resolved_name,
                    name_source = resolved_name
                        and "CardGameCardInfo.NameID->Text/US/Resident_TxtRes" or nil,
                }
            end
        end
    end
    -- Fail closed on an unavailable or ambiguous flag.  A unique true flag
    -- is the only manager-owned cursor state promoted to the public cursor;
    -- CardIndex remains slot diagnostics and is never treated as card ID.
    if cursor_observed and cursor_true_count == 1 and cursor_slot ~= nil then
        live_card_focus_slot = cursor_slot
        live_card_focus_source = "manager.CardHandActors[].bCursorVisible"
    elseif cursor_observed then
        live_card_focus_slot = nil
        live_card_focus_source = nil
    end
    table.sort(used_indices)
    local parts = {}
    for position, value in ipairs(used_indices) do parts[position] = tostring(value) end
    hand_ids.seen = table.concat(parts, ",")
    hand_ids.missing = identity_missing
    return hand
end

-- The card catalogue is a concrete runtime data-object family, not a widget
-- tree.  Probe only the known Queen's Blood data-object classes and only the
-- fields which can identify a loaded table.  This does not walk RowMap or
-- enumerate arbitrary UObject instances.
local function debug_card_data_objects()
    local now = os.time()
    if now - last_card_data_object_debug_at < 15 then return end
    last_card_data_object_debug_at = now
    local classes = {
        "EndDataObjectCardGameCardInfo",
        "EndDataObjectCardGame",
        "EndDataObjectCardGamePresetDeck",
        "EndDataObjectCardGameOpponentDeck",
    }
    local fields = {
        "RowStruct", "RowMap", "NameID", "UniqueId", "CardId", "CardID",
        "CardSet_Array", "cardID_Array",
    }
    for _, class_name in ipairs(classes) do
        local objects = safe(nil, function() return FindAllOf(class_name) end)
        local count = type(objects) == "table" and #objects or 0
        write_debug(GAME_DEBUG_PATH, "card_data_object_count", class_name .. "|" .. tostring(count))
        if type(objects) == "table" then
            for index = 1, math.min(count, 8) do
                local object = objects[index]
                if valid(object) then
                    local full_name = text(safe(nil, function() return object:GetFullName() end)) or ""
                    write_debug(GAME_DEBUG_PATH, "card_data_object", class_name .. "|" .. full_name)
                    for _, field in ipairs(fields) do
                        local value = safe(nil, function() return object:GetPropertyValue(field) end)
                        if value == nil then
                            value = safe(nil, function() return object[field] end)
                        end
                        if value ~= nil then
                            write_debug(
                                GAME_DEBUG_PATH,
                                "card_data_object_field",
                                full_name .. "|" .. field .. "|type=" .. type(value) .. "|" .. (text(value) or "<non_text_value>")
                            )
                        end
                    end
                    local method_names = {"GetRowNames", "GetRowStruct", "FindRowUnchecked", "GetRowMap"}
                    for _, method_name in ipairs(method_names) do
                        local method = safe(nil, function() return object[method_name] end)
                        if type(method) == "function" then
                            write_debug(
                                GAME_DEBUG_PATH,
                                "card_data_object_method",
                                full_name .. "|" .. method_name .. "|callable"
                            )
                        end
                    end
                end
            end
        end
    end
end

-- CardGame_Card_Full's authored Billboard_Thumbnail is the one known
-- presentation boundary whose native schema contains an FSoftObjectPath.
-- Read only that direct property from the five named live cards. While the
-- card-game UI is present, also read the authored Txt_CardName.Text label and
-- join its exact value to the offline catalogue. Never derive a card identity
-- from a widget/object name.
local function debug_live_card_image_identity(known_root, allow_card_name_identity)
    local now = os.time()
    -- Re-read the five exact live billboards frequently enough that a
    -- mulligan replacement cannot retain the previous slot identity for a
    -- whole telemetry window.  The scan remains class-bounded and capped.
    if now - last_card_image_identity_debug_at < 1 then return end
    last_card_image_identity_debug_at = now
    live_card_probe = {
        root_count = 0,
        root_path_count = 0,
        full_candidate_count = 0,
        full_valid_count = 0,
        full_live_match_count = 0,
        full_slot_match_count = 0,
        visual_widget_count = 0,
        billboard_count = 0,
        image_path_count = 0,
        asset_path_count = 0,
        identity_count = 0,
        slot_diagnostics = {},
        root_path = nil,
        first_full_name = nil,
        first_live_full_name = nil,
    }

    -- The live prompt belongs to CardGame_Top_C while its five card widgets
    -- may be constructed under CardGame_Test_C. Keep both exact roots so the
    -- direct card lookup can match the actual live hand path.
    local roots = {}
    local root_names = {}
    local function append_root(object)
        if #roots >= 2 or not is_live_card_hand_root(object) then return end
        local full_name = text(safe(nil, function() return object:GetFullName() end)) or ""
        local lowered = full_name:lower()
        for _, prior_name in ipairs(root_names) do
            if prior_name == lowered then return end
        end
        roots[#roots + 1] = object
        root_names[#root_names + 1] = lowered
    end

    -- Reuse the exact constructed root that already passed the screen
    -- presence check.  That check may have found the live object through its
    -- cooked path even when FindAllOf(class) does not return it in the same
    -- tick. A second bounded root is allowed because the top-level prompt and
    -- the live card widgets are authored under different concrete classes.
    append_root(known_root)
    local roots_fresh = #cached_hand_roots > 0
        and now - cached_hand_roots_at < HAND_SCAN_REFRESH_SECONDS
    if roots_fresh then
        for _, path in ipairs(cached_hand_roots) do
            if refs.resolve(path) == nil then roots_fresh = false; break end
        end
    end
    if roots_fresh then
        for _, path in ipairs(cached_hand_roots) do append_root(refs.resolve(path)) end
    else
        cached_hand_roots = {}
        cached_hand_roots_at = now
        for _, root_class in ipairs({"CardGame_Top_C", "CardGame_Test_C"}) do
            local objects = safe(nil, function() return FindAllOf(root_class) end)
            if type(objects) == "table" then
                for index = 1, math.min(#objects, 8) do
                    local object = objects[index]
                    if is_live_card_hand_root(object) then
                        append_root(object)
                        local path = refs.path(object)
                        if path then cached_hand_roots[#cached_hand_roots + 1] = path end
                        break
                    end
                end
            end
        end
    end
    if #roots == 0 then
        live_card_identity_by_slot = {}
        live_card_focus_slot = nil
        live_card_focus_source = nil
        cached_hand_actors = nil
        hand_ids.read = nil
        hand_ids.read_at = nil
        card_widget_reference_cache = {}
        cached_hand_roots = {}
        cached_full_widgets = nil
        write_debug(GAME_DEBUG_PATH, "card_image_identity_root_count", "0")
        return
    end
    write_debug(GAME_DEBUG_PATH, "card_image_identity_root_count", tostring(math.min(#roots, 2)))
    local live_root_paths = {}
    live_card_identity_by_slot = {}
    local function merge_identity(slot, identity)
        if identity == nil then return end
        local prior = live_card_identity_by_slot[slot]
        if prior ~= nil and prior.identity_conflict then return end
        -- Token cards have no Card_### asset key; compare card ids.
        if prior ~= nil and prior.card_id ~= nil and identity.card_id ~= nil
            and prior.card_id ~= identity.card_id then
            live_card_identity_by_slot[slot] = {
                identity_conflict = true,
                mulligan_selected = prior.mulligan_selected,
                mulligan_selection_source = prior.mulligan_selection_source,
                mulligan_visuals = prior.mulligan_visuals,
                mulligan_hovered = prior.mulligan_hovered,
                mulligan_hover_source = prior.mulligan_hover_source,
                mulligan_hover_state = prior.mulligan_hover_state,
            }
            write_debug(
                GAME_DEBUG_PATH,
                "card_identity_conflict",
                tostring(slot) .. "|first=" .. tostring(prior.card_id)
                    .. "|second=" .. tostring(identity.card_id)
            )
            return
        end
        if prior ~= nil then
            identity.card_name = identity.card_name or prior.card_name
            identity.name_source = identity.name_source or prior.name_source
            identity.mulligan_selected = prior.mulligan_selected
            identity.mulligan_selection_source = prior.mulligan_selection_source
            identity.mulligan_visuals = prior.mulligan_visuals
            identity.mulligan_hovered = prior.mulligan_hovered
            identity.mulligan_hover_source = prior.mulligan_hover_source
            identity.mulligan_hover_state = prior.mulligan_hover_state
        end
        live_card_identity_by_slot[slot] = identity
    end

    -- Resolve every root path first so the CardFull scan runs once per pass
    -- with the complete root set, rather than once per root.
    local root_paths = {}
    for root_index = 1, math.min(#roots, 2) do
        local root = roots[root_index]
        if valid(root) then
            local root_name = text(safe(nil, function() return root:GetFullName() end)) or ""
            root_paths[root_index] = string.match(root_name, " (/.*)$")
            if root_paths[root_index] ~= nil then
                live_root_paths[#live_root_paths + 1] = root_paths[root_index]:lower()
            end
        end
    end
    local live_full_widgets, live_widget_names, live_widget_paths =
        live_card_full_widgets(live_root_paths)

    for root_index = 1, math.min(#roots, 2) do
        -- The widgets are shared by both roots; once one pass has resolved
        -- all five, a second pass would only repeat the same reads.
        if root_index > 1 and live_card_probe.visual_widget_count >= CARD_WIDGET_POOL_SIZE then break end
        local root = roots[root_index]
        if valid(root) then
            local root_path = root_paths[root_index]
            if root_path ~= nil then
                live_card_probe.root_path_count = live_card_probe.root_path_count + 1
                if live_card_probe.root_path == nil then live_card_probe.root_path = root_path end
                write_debug(GAME_DEBUG_PATH, "card_image_identity_root", root_path)
                for slot = 1, CARD_WIDGET_POOL_SIZE do
                    local visual_widget = live_full_widgets[slot]
                    local widget_name = live_widget_names[slot]
                    local widget_path = live_widget_paths[slot]
                    if widget_name == nil then
                        widget_name = string.format("CardFull_%02d", slot)
                    end
                    -- Prefer the constructed object found by the bounded class
                    -- scan; if that scan misses, try only the authored name.
                    if not valid(visual_widget) then
                        local child = safe(nil, function()
                            return StaticFindObject(root_path .. ".WidgetTree." .. widget_name)
                        end)
                        if valid(child) then
                            visual_widget = child
                            widget_path = root_path .. ".WidgetTree." .. widget_name
                        end
                    end
                    if visual_widget ~= nil then
                        live_card_probe.visual_widget_count = live_card_probe.visual_widget_count + 1
                        local slot_probe = live_card_probe.slot_diagnostics[slot] or {}
                        slot_probe.hand_index = slot - 1
                        slot_probe.widget_name = widget_name
                        slot_probe.visual_lookup_root_path = root_path
                        slot_probe.widget_found = valid(visual_widget)
                        live_card_probe.slot_diagnostics[slot] = slot_probe
                        -- Do not call GetFullName/GetClass on this generated
                        -- child: that exact access crashed a prior probe.
                        if allow_card_name_identity then
                            -- The extracted CardGame_Card_Full class names
                            -- Txt_CardName as an EndTextBlock child. Read that
                            -- one authored label while the bounded card-game
                            -- UI is present, then join its full English name
                            -- against the offline card catalogue.
                            local name_widget = select(1, read_field(visual_widget, {"Txt_CardName"}))
                            slot_probe.card_name_widget_found = valid(name_widget)
                            local card_name_value = nil
                            local card_name_value_source = nil
                            if valid(name_widget) then
                                card_name_value, card_name_value_source = read_field(name_widget, {"Text"})
                                if card_name_value == nil then
                                    card_name_value, card_name_value_source = call_method(name_widget, {"GetText"})
                                end
                            end
                            local card_name_text = text(card_name_value)
                            local name_identity = card_identity_from_name(card_name_text)
                            slot_probe.card_name_value_type = card_name_value ~= nil and type(card_name_value) or nil
                            slot_probe.card_name_text_source = card_name_value_source
                            slot_probe.card_name_text = card_name_text ~= nil and card_name_text:sub(1, 128) or nil
                            slot_probe.card_name_text_read = card_name_text ~= nil
                            slot_probe.card_name_identity_resolved = name_identity ~= nil
                            if name_identity ~= nil then
                                slot_probe.card_name_card_id = name_identity.card_id
                                slot_probe.card_name_asset_key = name_identity.asset_key
                                live_card_probe.identity_count = live_card_probe.identity_count + 1
                                merge_identity(slot - 1, name_identity)
                                write_debug(
                                    GAME_DEBUG_PATH,
                                    "card_name_identity_resolved_direct",
                                    tostring(slot - 1)
                                        .. "|card_id=" .. name_identity.card_id
                                        .. "|asset_key=" .. name_identity.asset_key
                                        .. "|name=" .. name_identity.card_name
                                )
                            end
                        end
                        -- Pnl_Mulligan.Visibility is the only marker source. An
                        -- unresolved panel or undecodable enum stays nil
                        -- (unknown); it is never reported as unmarked.
                        local marker, marker_source, marker_state =
                            read_mulligan_marker(visual_widget, widget_path)
                        slot_probe.widget_path = widget_path
                        slot_probe.mulligan_selected = marker
                        slot_probe.mulligan_selection_source = marker_source
                        slot_probe.mulligan_visuals = marker_state
                        local slot_identity = live_card_identity_by_slot[slot - 1] or {}
                        slot_identity.mulligan_selected = marker
                        slot_identity.mulligan_selection_source = marker_source
                        slot_identity.mulligan_visuals = marker_state
                        live_card_identity_by_slot[slot - 1] = slot_identity
                        if marker ~= nil then
                            write_debug(
                                GAME_DEBUG_PATH,
                                "mulligan_card_selection",
                                tostring(slot - 1) .. "|source=" .. marker_source
                                    .. "|selected=" .. tostring(marker)
                            )
                        end
                        local billboard = safe(nil, function() return visual_widget:GetPropertyValue("Billboard_Thumbnail") end)
                        if billboard == nil then
                            billboard = safe(nil, function() return visual_widget.Billboard_Thumbnail end)
                        end
                        if billboard == nil then
                            billboard = safe(nil, function()
                                return StaticFindObject(root_path .. ".WidgetTree." .. widget_name .. ".Billboard_Thumbnail")
                            end)
                        end
                        if billboard == nil then
                            billboard = safe(nil, function()
                                return StaticFindObject(
                                    root_path .. ".WidgetTree." .. widget_name .. ".WidgetTree.Billboard_Thumbnail"
                                )
                            end)
                        end
                        -- A nested UserWidget's children can be named with
                        -- either the UObject dot form or the generated
                        -- instance colon form.  These are the finite authored
                        -- paths for this one Billboard_Thumbnail; do not turn
                        -- this into a widget-tree traversal.
                        if billboard == nil then
                            billboard = safe(nil, function()
                                return StaticFindObject(
                                    root_path .. ".WidgetTree." .. widget_name .. ":WidgetTree.Billboard_Thumbnail"
                                )
                            end)
                        end
                        if billboard == nil then
                            billboard = safe(nil, function()
                                return StaticFindObject(
                                    root_path .. ".WidgetTree." .. widget_name .. "_C:WidgetTree.Billboard_Thumbnail"
                                )
                            end)
                        end
                        if billboard == nil then
                            billboard = safe(nil, function()
                                return StaticFindObject(
                                    root_path .. ".WidgetTree." .. widget_name .. "_C_0:WidgetTree.Billboard_Thumbnail"
                                )
                            end)
                        end
                        if valid(billboard) then
                            live_card_probe.billboard_count = live_card_probe.billboard_count + 1
                            slot_probe.billboard_found = true
                            local image_path = safe(nil, function() return billboard:GetPropertyValue("ImagePath") end)
                            if image_path == nil then
                                image_path = safe(nil, function() return billboard.ImagePath end)
                            end
                            local path_asset = nil
                            local path_sub = nil
                            if image_path ~= nil then
                                live_card_probe.image_path_count = live_card_probe.image_path_count + 1
                                path_asset = safe(nil, function() return image_path.AssetPathName end)
                                path_sub = safe(nil, function() return image_path.SubPathString end)
                            end
                            if path_asset ~= nil then live_card_probe.asset_path_count = live_card_probe.asset_path_count + 1 end
                            local path_asset_text = text(path_asset)
                            local path_sub_text = text(path_sub)
                            slot_probe.image_path_found = image_path ~= nil
                            slot_probe.image_path_type = image_path ~= nil and type(image_path) or nil
                            slot_probe.asset_path_name_found = path_asset ~= nil
                            slot_probe.asset_path_name_type = path_asset ~= nil and type(path_asset) or nil
                            slot_probe.asset_path_name_text = path_asset_text ~= nil and path_asset_text:sub(1, 192) or nil
                            slot_probe.sub_path_string_type = path_sub ~= nil and type(path_sub) or nil
                            slot_probe.sub_path_string_text = path_sub_text ~= nil and path_sub_text:sub(1, 192) or nil
                            local identity = card_identity_from_asset_path(path_asset_text)
                            slot_probe.asset_path_identity_resolved = identity ~= nil
                            if identity ~= nil then
                                slot_probe.asset_path_card_id = identity.card_id
                                slot_probe.asset_path_asset_key = identity.asset_key
                                live_card_probe.identity_count = live_card_probe.identity_count + 1
                                merge_identity(slot - 1, identity)
                                write_debug(
                                    GAME_DEBUG_PATH,
                                    "card_image_identity_resolved_direct",
                                    tostring(slot - 1)
                                        .. "|card_id=" .. identity.card_id
                                        .. "|asset_key=" .. tostring(identity.asset_key)
                                        .. "|image_id=" .. identity.image_id
                                )
                            end
                            write_debug(
                                GAME_DEBUG_PATH,
                                "card_image_identity",
                                -- ImagePath is a reflected FSoftObjectPath
                                -- struct. Log its Lua type only; generic
                                -- ToString/tostring coercion is unsupported.
                                tostring(slot - 1) .. "|image_path_type=" .. type(image_path)
                            )
                        else
                            write_debug(GAME_DEBUG_PATH, "card_image_identity", tostring(slot - 1) .. "|billboard_unreadable")
                        end
                    else
                        write_debug(
                            GAME_DEBUG_PATH,
                            "card_image_identity",
                            tostring(slot - 1) .. "|lookup_ok=" .. tostring(static_ok) .. "|child=false"
                        )
                    end
                end
            else
                write_debug(GAME_DEBUG_PATH, "card_image_identity_root_path", "unavailable")
            end
        end
    end

    -- CardGame_Test_C derives from native UEndCardGame3DMenu, whose reflected
    -- _DeckCard array holds the hand's card panes. Report only their paths so
    -- a live run can confirm the identity/marker widgets are those panes; the
    -- array is never used to choose or reorder slots. Read it only on the
    -- CardGame_Test_C root: elsewhere the missing member is a nullptr UObject,
    -- and GetArrayNum on it makes UE4SS log a full stack trace every tick,
    -- which starved the Begin Match transition in live validation.
    for root_index = 1, math.min(#roots, 2) do
        local root_name = text(safe(nil, function() return roots[root_index]:GetFullName() end)) or ""
        local deck = nil
        if root_name:match("^CardGame_Test_C ") then
            deck = safe(nil, function() return roots[root_index]._DeckCard end)
        end
        local entries = reflected_array_items(deck, 5)
        if #entries > 0 then
            local deck_cards = {}
            for _, entry in ipairs(entries) do
                deck_cards[#deck_cards + 1] = valid(entry.object) and object_path(entry.object) or false
            end
            live_card_probe.deck_card_paths = deck_cards
            break
        end
    end

    local direct_identity_complete = true
    for slot = 0, 4 do
        if live_card_identity_by_slot[slot] == nil then
            direct_identity_complete = false
            break
        end
    end

    -- If the generated CardFull property wrapper is not exposed, inspect the
    -- exact native widget class that owns the authored ImagePath instead. Do
    -- this only after a complete hand was observed or a positive mulligan
    -- marker was read. Deck selection also owns CardFull-looking widgets, and
    -- scanning every EndBillboard there can stall the game-thread exporter
    -- before the real hand exists.
    local direct_mulligan_marker = false
    for slot = 0, 4 do
        local identity = live_card_identity_by_slot[slot]
        if identity ~= nil and identity.mulligan_selected == true then
            direct_mulligan_marker = true
            break
        end
    end
    if not LIVE_BILLBOARD_FALLBACK_ENABLED
        or direct_identity_complete
        or (not manager_hand_seen and not direct_mulligan_marker) then
        return
    end
    live_card_probe.root_count = #roots
    for _, billboard_class in ipairs({"EndBillboard", "EndBillboard_C"}) do
        local billboards = safe(nil, function() return FindAllOf(billboard_class) end)
        write_debug(
            GAME_DEBUG_PATH,
            "card_image_identity_billboard_result",
            billboard_class .. "|type=" .. type(billboards)
                .. "|count=" .. (type(billboards) == "table" and tostring(#billboards) or "na")
        )
        if type(billboards) == "table" then
            for index = 1, math.min(#billboards, 512) do
                local billboard = billboards[index]
                if valid(billboard) then
                    local full_name = text(safe(nil, function() return billboard:GetFullName() end)) or ""
                    local lowered = full_name:lower()
                    -- The first entries include persistent menu-card widgets;
                    -- only the finite, authored MiniGame/CardGame outer path
                    -- is relevant to the live mulligan hand.  Avoid reading
                    -- ImagePath on the unrelated MainMenu CardFull objects:
                    -- that property read stalled the UE4SS callback in an
                    -- earlier probe.
                    local belongs_to_live_root = false
                    for _, root_path in ipairs(live_root_paths) do
                        if lowered:find(root_path, 1, true) then
                            belongs_to_live_root = true
                            break
                        end
                    end
                    if belongs_to_live_root then
                        write_debug(
                            GAME_DEBUG_PATH,
                            "card_image_identity_object_name",
                            tostring(index) .. "|" .. (full_name ~= "" and full_name or "<unreadable>")
                        )
                        -- FSoftObjectPath is a reflected value, not a UObject.
                        -- On this live EndBillboard the generic property
                        -- getter stalled the callback, so use the direct
                        -- member first and inspect only its two path fields.
                        local image_path = safe(nil, function() return billboard.ImagePath end)
                        local path_asset = nil
                        local path_sub = nil
                        if image_path ~= nil then
                            path_asset = safe(nil, function() return image_path.AssetPathName end)
                            path_sub = safe(nil, function() return image_path.SubPathString end)
                        end
                        write_debug(
                            GAME_DEBUG_PATH,
                            "card_image_identity_object",
                            full_name .. "|image_path_type=" .. type(image_path)
                                .. "|asset_path_name=" .. (text(path_asset) or "<unreadable>")
                                .. "|sub_path=" .. (text(path_sub) or "<unreadable>")
                        )
                        local slot_text = lowered:match("cardfull_(%d+).*billboard_thumbnail")
                        local slot = integer(slot_text)
                        if slot ~= nil and slot >= 1 and slot <= 5 then
                            local identity = card_identity_from_asset_path(path_asset)
                            if identity ~= nil then
                                merge_identity(slot - 1, identity)
                                write_debug(
                                    GAME_DEBUG_PATH,
                                    "card_image_identity_resolved",
                                    tostring(slot - 1)
                                        .. "|card_id=" .. identity.card_id
                                        .. "|asset_key=" .. tostring(identity.asset_key)
                                        .. "|image_id=" .. identity.image_id
                                )
                            end
                        end
                    end
                end
            end
        end
    end
end

-- During the deal-to-mulligan transition the manager can be destroyed after
-- it has published the drawn hand, while the five visible CardFull widgets
-- remain alive. The artwork billboard is the stable identity boundary for
-- those widgets. Reconstruct only the exact five hand slots from the
-- already-resolved Card_### asset keys; never use the widget name as a card
-- identity and never scrape the finished text tree.
local function live_identity_count()
    local count = 0
    for slot = 0, 4 do
        local identity = live_card_identity_by_slot[slot]
        if identity ~= nil and identity.card_id ~= nil then count = count + 1 end
    end
    return count
end

local function encode_live_identity_hand()
    local hand = {}
    for slot = 0, 4 do
        local identity = live_card_identity_by_slot[slot]
        local catalogue_entry = catalogue_entry_for_identity(identity)
        if identity ~= nil and catalogue_entry ~= nil then hand[#hand + 1] = {
            hand_index = slot,
            card_id = identity.card_id,
            card_image_id = identity.image_id,
            card_unique_id = integer(catalogue_entry.unique_id),
            card_name_id = text(catalogue_entry.name_id),
            cost = integer(catalogue_entry.cost),
            power = integer(catalogue_entry.power),
            legal_positions = catalogue_entry.legal_positions,
            influence = text(catalogue_entry.influence),
            note_id = text(catalogue_entry.note_id),
            effect_id = text(catalogue_entry.effect_id),
            effect_text_id = text(catalogue_entry.effect_text_id),
            effect_influence = text(catalogue_entry.effect_influence),
            effect_constant = number(catalogue_entry.effect_constant),
            effect_target = integer(catalogue_entry.effect_target),
            effect_type = integer(catalogue_entry.effect_type),
            effect_int_parameters = catalogue_entry.effect_int_parameters,
            effect_text_parameters = catalogue_entry.effect_text_parameters,
            effect_reference_text_id = text(catalogue_entry.effect_reference_text_id),
            effect_text = text(catalogue_entry.effect_text),
            effect_text_source = text(catalogue_entry.effect_text_source),
            effect_type_name = text(catalogue_entry.effect_type_name),
            effect_target_name = text(catalogue_entry.effect_target_name),
            effect_text_label_references = catalogue_entry.effect_text_label_references,
            effect_heading_types = catalogue_entry.effect_heading_types,
            effect_parameter_references = catalogue_entry.effect_parameter_references,
            effect_reference_counts = catalogue_entry.effect_reference_counts,
            effect_filter_tags = catalogue_entry.effect_filter_tags,
            effect_reference_count0 = integer(catalogue_entry.effect_reference_count0),
            effect_card_definitions = effect_card_definitions(catalogue_entry),
            influence_square_count = integer(catalogue_entry.influence_square_count),
            influence_offsets = catalogue_entry.influence_offsets,
            effect_influence_square_count = integer(catalogue_entry.effect_influence_square_count),
            effect_influence_offsets = catalogue_entry.effect_influence_offsets,
            card_index = last_card_indices[slot],
            mulligan_selected = identity.mulligan_selected,
            mulligan_selection_source = identity.mulligan_selection_source,
            mulligan_hovered = identity.mulligan_hovered,
            mulligan_hover_source = identity.mulligan_hover_source,
            mulligan_hover_candidates = identity.mulligan_hover_state,
            mulligan_visuals = identity.mulligan_visuals,
            card_index_source = "hand_actor.CardIndex(slot_only)",
            card_identity_source = identity.identity_source
                or "CardFull_0N.Billboard_Thumbnail.ImagePath.AssetPathName",
            name = text(identity.card_name) or text(catalogue_entry.name),
            name_source = identity.name_source
                or "CardGameCardInfo.NameID->Text/US/Resident_TxtRes",
        } end
    end
    return hand
end

-- A selected Pnl_Mulligan is a direct screen-state boundary. It is safe to
-- use this only as an affirmative signal: an unmarked CardFull is not enough
-- to distinguish the deck selector from the opening hand, but one visible
-- mulligan marker proves that the five authored CardFull slots are live
-- mulligan cards even if the manager hand was torn down before this sample.
local function live_mulligan_marker_observed()
    for slot = 0, 4 do
        local identity = live_card_identity_by_slot[slot]
        if identity ~= nil and identity.mulligan_selected == true then
            return true
        end
    end
    return false
end

-- Resolve the keyboard-hovered card only from one consistent direct state
-- source across all five live CardFull instances.  A source is accepted only
-- when every slot reports a Boolean and exactly one slot is true.  This keeps
-- a mouse-only IsHovered() result, an always-visible image reference, or an
-- ambiguous transition from being mistaken for the game cursor.
local LIVE_CARD_HOVER_SOURCES = {
    "CardFull_0N.bHovered",
    "CardFull_0N.IsHovered",
    "CardFull_0N.Hovered",
    "CardFull_0N.bIsHovered",
    "CardFull_0N.bFocused",
    "CardFull_0N.IsFocused",
    "CardFull_0N.Focused",
    "CardFull_0N.bSelected",
    "CardFull_0N.IsSelected",
    "CardFull_0N.Selected",
    "CardFull_0N.IsHovered()",
    "CardFull_0N.HasKeyboardFocus()",
    "CardFull_0N.HasFocusedDescendants()",
    "CardFull_0N.HasAnyUserFocus()",
    "CardFull_0N.HasUserFocus(0)",
    "CardFull_0N.GetFocusedChildWidget()",
    "CardFull_0N.Card_Selected.IsVisible()",
    "CardFull_0N.Card_Selected.Visibility",
}

local function encode_live_card_cursor()
    if live_card_focus_slot ~= nil and live_card_identity_by_slot[live_card_focus_slot] ~= nil then
        return {
            area = "hand",
            hand_index = live_card_focus_slot,
            row = nil,
            column = nil,
            source = live_card_focus_source or "focus_hook:CardFull_0N",
        }
    end
    for _, source in ipairs(LIVE_CARD_HOVER_SOURCES) do
        local available = true
        local true_count = 0
        local hovered_index = nil
        for slot = 0, 4 do
            local identity = live_card_identity_by_slot[slot]
            local state = identity and identity.mulligan_hover_state or nil
            local entry = state and state[source] or nil
            if entry == nil or entry.value == nil then
                available = false
                break
            end
            if entry.value then
                true_count = true_count + 1
                hovered_index = slot
            end
        end
        if available and true_count == 1 then
            return {
                area = "hand",
                hand_index = hovered_index,
                row = nil,
                column = nil,
                source = source,
            }
        end
    end
    return {
        area = "unknown",
        hand_index = nil,
        row = nil,
        column = nil,
        source = "CardFull_0N.direct_hover_state_unresolved",
    }
end

local function normalise_owner(value)
    local owner = (text(value) or "unknown"):lower()
    if owner:find("player") or owner:find("self") then return "player" end
    if owner:find("opponent") or owner:find("enemy") then return "opponent" end
    if owner:find("contest") then return "contested" end
    if owner:find("neutral") then return "neutral" end
    if owner:find("empty") then return "empty" end
    return "unknown"
end

local function encode_move(object)
    local hand_index = integer(select(1, read_field(object, {"HandIndex", "CardIndex", "CardSlot"})))
    local row = integer(select(1, read_field(object, {"Row", "BoardRow"})))
    local column = integer(select(1, read_field(object, {"Column", "BoardColumn", "Col"})))
    if hand_index == nil or row == nil or column == nil or row < 0 or row > 2 or column < 0 or column > 4 then return nil end
    return {
        hand_index = hand_index,
        card_id = text(select(1, read_field(object, {"CardId", "CardID"}))),
        row = row,
        column = column,
        score = number(select(1, read_field(object, {"Score", "Priority", "Evaluation"}))),
        legal = boolean(select(1, read_field(object, {"Legal", "IsLegal"}))) ~= false,
    }
end

local function encode_cell(object)
    local item = position(object)
    if not item then return nil end
    local owner = normalise_owner(select(1, read_field(object, {"Owner", "Side", "Control"})))
    return {
        row = item.row,
        column = item.column,
        owner = owner,
        card_id = text(select(1, read_field(object, {"CardId", "CardID", "Id"}))),
        power = integer(select(1, read_field(object, {"Power", "CardPower"}))),
        control = integer(select(1, read_field(object, {"ControlCount", "Control"}))),
    }
end

local function encode_cursor(manager, ui)
    local object = ui or manager
    local area = text(select(1, read_field(object, {"CursorArea", "Focus", "SelectedArea"}))) or "unknown"
    area = area:lower()
    if area ~= "hand" and area ~= "board" then area = "unknown" end
    return {
        area = area,
        hand_index = integer(select(1, read_field(object, {"CursorHandIndex", "SelectedHandIndex", "HandIndex"}))),
        row = integer(select(1, read_field(object, {"CursorRow", "SelectedRow", "Row"}))),
        column = integer(select(1, read_field(object, {"CursorColumn", "SelectedColumn", "Column"}))),
    }
end

local function normalise_phase(value)
    local phase = (text(value) or ""):lower()
    if phase:find("player") and phase:find("turn") then return "player_turn" end
    if phase:find("opponent") and phase:find("turn") then return "opponent_turn" end
    if phase:find("resolv") then return "resolving" end
    if phase:find("deal") then return "dealing" end
    if phase:find("enter") then return "entering" end
    if phase:find("result") or phase:find("finished") then return "result" end
    if phase == "inactive" or phase == "none" then return "inactive" end
    return "unknown"
end

local function normalise_screen(value)
    local screen = (text(value) or ""):lower()
    if screen == "" then return nil end
    if screen:find("prematch") or screen:find("pre%-match") or screen:find("pre_match") then
        return "pre_match"
    end
    if screen:find("matchmenu") or screen:find("match%-menu") or screen:find("match_menu") then
        return "match_menu"
    end
    if screen:find("beginmatch") or screen:find("begin%-match") or screen:find("begin_match") then
        return "begin_match"
    end
    if screen:find("startmatch") or screen:find("start%-match") or screen:find("start_match") then
        return "start_match"
    end
    if screen:find("deck") then return "deck_select" end
    return screen:gsub("%s+", "_")
end

local function read_ui_or_manager(ui, manager, names)
    return read_field(manager, names)
end

-- Re-read only bCursorVisible on the cached hand actors, with the same
-- unique-true rule as encode_manager_hand. Any invalid actor drops the cache.
-- Board state from the 3D manager. AEndCardGame3DManager.FieldInfos holds one
-- FEndCardGame3DFieldInfo per cell (index = row * 5 + column): the lengths of
-- FlagPlayers / FlagOpponents are the pawn rank and owner, and bCursorVisible
-- lights the cells the focused hand card may be placed on. CardBoardActors
-- has one slot per cell holding the placed card's actor. While a card is being
-- placed (CardPlacementActor valid) the pawn flags show the game's preview of
-- the placement, so the last pre-preview flags are published instead and the
-- target is the lit cell whose preview flags dropped to zero.
local BOARD_CELLS = 15
local board_flag_cache = {}
local board_card_owner = {}
local board_owner_manager = nil

local function array_length(value)
    return integer(safe(nil, function() return value:GetArrayNum() end))
end

-- Each cell is drawn by CardGame_Test_C.CardFrame_{cell+1}; its CardSmall
-- (CardGame_Card_Small_C) shows the placed card: Billboard_Thumbnail's art
-- path is the card identity and NumLabel.Value is the current (buffed or
-- debuffed) power. The two widget references are cached per cell.
local board_face_cache = {}
local board_cursor_cache = {}
local demand = {census = {key = nil, cells = {}, boxes = {}}}
-- Primitive root state only. A failed native path-cache read is not proof
-- that the board's authored UI was destroyed; reacquire that exact class
-- once per second, then read its current fields. Never retain UObjects or
-- reuse an older identity/power when a face is unreadable.
demand.match_root = {path = nil, identity = nil, lookup_at = nil}

function demand.constructed_test_root(root)
    if not valid(root) then return nil end
    local name = text(safe(nil, function() return root:GetFullName() end))
    if name == nil or not name:match("^CardGame_Test_C /Engine/Transient%.") then return nil end
    return name:match(" (/.*)$")
end

function demand.use_test_root(root)
    local path = demand.constructed_test_root(root)
    if path == nil then return nil, nil end
    local address = safe(nil, function() return string.format("%x", root:GetAddress()) end)
    if address == nil or address == "0" then return nil, nil end
    local identity = path .. "@" .. address
    if demand.match_root.identity ~= identity then
        board_face_cache, board_cursor_cache = {}, {}
    end
    demand.match_root.path, demand.match_root.identity = refs.path(root), identity
    return root, path
end

local function live_test_root()
    local root = refs.resolve(demand.match_root.path)
    if demand.constructed_test_root(root) ~= nil then return demand.use_test_root(root) end
    for _, path in ipairs(cached_hand_roots) do
        root = refs.resolve(path)
        if demand.constructed_test_root(root) ~= nil then return demand.use_test_root(root) end
    end
    local now = os.time()
    if demand.match_root.lookup_at ~= nil and now - demand.match_root.lookup_at < 1 then return nil, nil end
    demand.match_root.lookup_at = now
    local objects = safe(nil, function() return FindAllOf("CardGame_Test_C") end)
    if type(objects) ~= "table" or #objects > 8 then return nil, nil end
    local candidate = nil
    for index = 1, #objects do
        local object = objects[index]
        if demand.constructed_test_root(object) ~= nil then
            -- A retained second constructed screen is ambiguous. Do not pick
            -- a board from inventory order or guess which screen is current.
            if candidate ~= nil then return nil, nil end
            candidate = object
        end
    end
    if candidate ~= nil then return demand.use_test_root(candidate) end
    return nil, nil
end

local function board_card_face(index, root, root_path)
    if root == nil then root, root_path = live_test_root() end
    if root == nil then return nil, nil end
    local entry = board_face_cache[index]
    local billboard = entry and refs.resolve(entry.billboard) or nil
    local number_widget = entry and refs.resolve(entry.number) or nil
    if billboard == nil or number_widget == nil then
        local name = string.format("CardFrame_%02d", index + 1)
        local frame = safe(nil, function() return root[name] end)
        if not valid(frame) and root_path ~= nil then
            frame = safe(nil, function() return StaticFindObject(root_path .. ".WidgetTree." .. name) end)
        end
        local small = valid(frame) and safe(nil, function() return frame.CardSmall end) or nil
        if not valid(small) then return nil, nil end
        billboard = safe(nil, function() return small.Billboard_Thumbnail end)
        number_widget = safe(nil, function() return small.NumLabel end)
        if not valid(billboard) or not valid(number_widget) then return nil, nil end
        entry = {billboard = refs.path(billboard), number = refs.path(number_widget)}
        board_face_cache[index] = entry
    end
    local asset = safe(nil, function() return billboard.ImagePath.AssetPathName end)
    local identity = card_identity_from_asset_path(text(asset))
    local power = integer(safe(nil, function() return number_widget.Value end))
    return identity, power
end

-- CardGame_Top_C.Result is the end-of-match screen (collapsed during play);
-- Win is the victory banner. Only once Result is shown are its score labels
-- and result text read.
local SHOWN_VISIBILITY = {[0] = true, [3] = true, [4] = true}

local function widget_shown(widget)
    if not valid(widget) then return nil end
    local visibility = integer(safe(nil, function() return widget.Visibility end))
    if visibility == nil then return nil end
    return SHOWN_VISIBILITY[visibility] == true
end

local function live_top_root()
    for _, path in ipairs(cached_hand_roots) do
        local root = refs.resolve(path)
        local name = valid(root) and text(safe(nil, function() return root:GetFullName() end)) or nil
        if name ~= nil and name:match("^CardGame_Top_C ") then return root end
    end
    local cached = refs.resolve(cached_ui_object)
    if cached ~= nil and cached_ui_class ~= nil
        and tostring(cached_ui_class):find("CardGame_Top_C", 1, true) then
        return cached
    end
    return nil
end

-- CardGame_Top_C.ConfirmWindow is the Begin Match "Start the match?" popup
-- and MulliganConfirm the mulligan's. nil means the widget was not readable.
local function encode_popups()
    local top = live_top_root()
    if top == nil then return nil end
    return {
        begin_confirm = widget_shown(safe(nil, function() return top.ConfirmWindow end)),
        mulligan_confirm = widget_shown(safe(nil, function() return top.MulliganConfirm end)),
    }
end

local function encode_match_result()
    local top = live_top_root()
    if top == nil then return {observed = false} end
    local result_widget = safe(nil, function() return top.Result end)
    local shown = widget_shown(result_widget)
    -- Only whether the result screen is up. Its Win widget reads shown and
    -- both score labels read 99 after wins and losses alike (live 2026-09-25
    -- and 2026-09-29), so they are not published; the harness scores the
    -- board instead (finish_queens_blood_match).
    return {
        observed = shown ~= nil,
        shown = shown == true,
    }
end

-- Pre-match deck screen (MainMenu_Card_TopNPC_C -> MainMenu_Card_Top_C, a
-- native UEndCardDeckWindow). Its lists are UEndVirtualScrollBox widgets:
-- ItemCount rows, GetSelectedIndex(0) for the cursor, and ContentWidgets for
-- the realised rows. Card grids are rows of UEndCardUnitList, each holding a
-- scroll box of MainMenu_Card_Card_Full_C cells whose Billboard_Thumbnail art
-- resolves through the catalogue's image index. Selection callbacks on the
-- window are hooked so the deck-slot and edit cursors are known.
local DECK_SCREEN_ROOT_CLASSES = {"MainMenu_Card_TopNPC_C"}
-- On some transitions FindAllOf omits the containing TopNPC widget even
-- though the visible MainMenu_Card_Top is still constructed. Recover via the
-- concrete child class, but require a live deck-specific button guide.
local DECK_SCREEN_REFRESH_SECONDS = 5
local DECK_SCREEN_MAX_ROWS = 24
local DECK_SCREEN_MAX_CELLS = 8
local deck_screen_cache = {root = nil, top = nil, scanned_at = nil, direct = false}
local deck_screen_events = {}
local deck_hooks_registered = false

-- A missing member is an invalid UObject, and calling any method on it
-- (ToString, getters) makes UE4SS log a stack trace every tick -- live, that
-- flooded the log and starved telemetry. Only touch validated members.
local function member(object, name)
    if not valid(object) then return nil end
    local value = safe(nil, function() return object[name] end)
    if not valid(value) then return nil end
    return value
end

local function member_text(object, name)
    local widget = member(object, name)
    if widget == nil then return nil end
    return text(safe(nil, function() return widget.Text end))
end

local function deck_screen_top(cached_only)
    local function deck_screen_guide_shown(top)
    local guide = member_text(top, "Txt_Guide")
    if type(guide) ~= "string" then return false end
    guide = guide:gsub("\194\160", " ")
    return guide:find("Change Deck", 1, true) ~= nil
        or guide:find("Jump to Card", 1, true) ~= nil
        or (guide:find("View Deck", 1, true) ~= nil
            and guide:find("Craft Prebuilt Deck", 1, true) ~= nil)
    end

    local top = refs.resolve(deck_screen_cache.top)
    local root = refs.resolve(deck_screen_cache.root)
    if top ~= nil and root ~= nil and widget_shown(top) == true
        and (not deck_screen_cache.direct
            or (card_ui_widget_shown(top) and deck_screen_guide_shown(top))) then
        return top
    end
    deck_screen_cache.top, deck_screen_cache.root, deck_screen_cache.direct = nil, nil, false
    if cached_only then return nil end
    local now = os.time()
    if deck_screen_cache.scanned_at ~= nil
        and now - deck_screen_cache.scanned_at < DECK_SCREEN_REFRESH_SECONDS then
        return nil
    end
    deck_screen_cache.scanned_at = now
    for _, class_name in ipairs(DECK_SCREEN_ROOT_CLASSES) do
        local found = safe(nil, function() return FindAllOf(class_name) end)
        for _, root in ipairs(type(found) == "table" and found or {}) do
            local name = text(safe(nil, function() return root:GetFullName() end)) or ""
            local top = member(root, "MainMenu_Card_Top")
            if top ~= nil and widget_shown(top) == true and not name:find("Default__", 1, true) then
                deck_screen_cache.root = refs.path(root)
                deck_screen_cache.top = refs.path(top)
                return top
            end
        end
    end
    -- Read only the named child class; never enumerate all UserWidgets.
    -- Its visible, deck-specific guide prevents a retained generic widget
    -- from making an unrelated menu look like Queen's Blood deck select.
    local found = safe(nil, function() return FindAllOf("MainMenu_Card_Top_C") end)
    for _, top in ipairs(type(found) == "table" and found or {}) do
        local name = text(safe(nil, function() return top:GetFullName() end)) or ""
        if not name:find("Default__", 1, true) and widget_shown(top) == true
            and card_ui_widget_shown(top) and deck_screen_guide_shown(top) then
            deck_screen_cache.root = refs.path(top)
            deck_screen_cache.top = refs.path(top)
            deck_screen_cache.direct = true
            return top
        end
    end
    return nil
end

local function register_deck_hooks()
    if deck_hooks_registered then return true end
    if type(RegisterHook) ~= "function" then return false end
    local hooks = {
        {"deck_list", "OnCradDeckListSelectedIndexChanged", 1},
        {"deck_edit", "OnCradDeckEditSelectedIndexChanged", 1},
        {"deck_edit_contents", "OnCradDeckEditContentsSelectedIndexChanged", 2},
        {"deck_contents", "OnCradDeckContentsSelectedIndexChanged", 1},
    }
    local registered = 0
    for _, hook in ipairs(hooks) do
        local key, function_name, arity = hook[1], hook[2], hook[3]
        local ok = pcall(function()
            RegisterHook("/Script/EndGame.EndCardDeckWindow:" .. function_name, function(_, first, second)
                local a = integer(safe(nil, function() return first:get() end))
                local b = arity == 2 and integer(safe(nil, function() return second:get() end)) or nil
                deck_screen_events[key] = {
                    index = arity == 2 and b or a,
                    row = arity == 2 and a or nil,
                    sequence = (deck_screen_events[key] and deck_screen_events[key].sequence or 0) + 1,
                }
            end)
        end)
        if ok then registered = registered + 1 end
    end
    deck_hooks_registered = registered > 0
    return deck_hooks_registered
end

local function deck_card_cell(widget)
    if not valid(widget) then return nil end
    local billboard = member(widget, "Billboard_Thumbnail")
    if billboard == nil then return nil end
    local asset = text(safe(nil, function() return billboard.ImagePath.AssetPathName end))
    local identity = card_identity_from_asset_path(asset)
    local entry = catalogue_entry_for_identity(identity)
    return {
        card_id = identity and identity.card_id or nil,
        name = entry and text(entry.name) or nil,
        image_id = identity and identity.image_id or asset,
        shown = widget_shown(widget),
        limit_text = member_text(widget, "Txt_Limit"),
        limit_shown = widget_shown(member(widget, "Pnl_Limit")),
        number = (function()
            local label = member(widget, "NumLabel")
            return label and integer(safe(nil, function() return label.Value end)) or nil
        end)(),
    }
end

local function deck_scroll_box(box, depth)
    if not valid(box) then return nil end
    local result = {
        item_count = integer(safe(nil, function() return box.ItemCount end)),
        selected = integer(safe(nil, function() return box:GetSelectedIndex(0) end)),
        rows = {},
    }
    local limit = depth == 0 and DECK_SCREEN_MAX_ROWS or DECK_SCREEN_MAX_CELLS
    local contents = reflected_array_items(select(1, read_field(box, {"ContentWidgets"})), limit)
    for _, item in ipairs(contents) do
        local widget = item.object
        if valid(widget) then
            local inner = member(widget, "EndVirtualScrollBox_103")
            if depth == 0 and inner ~= nil then
                local row = deck_scroll_box(inner, 1) or {}
                row.position = item.index
                result.rows[#result.rows + 1] = row
            else
                local cell = deck_card_cell(widget) or {
                    class = text(safe(nil, function() return widget:GetClass():GetFName():ToString() end)),
                }
                cell.position = item.index
                result.rows[#result.rows + 1] = cell
            end
        end
    end
    return result
end

local function deck_list_window(window, box_field)
    if not valid(window) then return nil end
    return {
        shown = widget_shown(window),
        title = member_text(window, "End_Text"),
        total = member_text(window, "Txt_TotalNumber"),
        enemy_name = member_text(window, "Txt_EnemyName"),
        list = deck_scroll_box(member(window, box_field), 0),
    }
end

-- Census of the deck screen's live card cells and scroll boxes. The object
-- path of a cell names its containers (deck list, owned grid, opponent
-- panel), so grouping by path needs no knowledge of the widget tree; each
-- cell also carries its "available/owned" limit text and whether its
-- CardCursor (the highlight) is shown. Bounded: only while the deck screen
-- is up, at most DECK_CENSUS_MAX_CELLS cells.
local DECK_CENSUS_MAX_CELLS = 64
local DECK_CENSUS_MAX_BOXES = 32

local function census_path(object)
    local full = text(safe(nil, function() return object:GetFullName() end)) or ""
    return full:match(" (.*)$") or full
end

-- A list's children through UEndVirtualScrollBox:GetChildAt, for lists whose
-- ContentWidgets array is not readable. A child holding its own scroll box
-- (a card row) is walked one level down; card cells are identified by art.
local DECK_CHILD_LIMIT = 16

local function box_children(box, depth)
    local count = integer(safe(nil, function() return box.ItemCount end)) or 0
    local children = {}
    for index = 0, math.min(count, DECK_CHILD_LIMIT) - 1 do
        local child = safe(nil, function() return box:GetChildAt(index) end)
        if valid(child) then
            local inner = member(child, "EndVirtualScrollBox_103")
            if depth == 0 and inner ~= nil then
                children[#children + 1] = {
                    position = index,
                    selected = integer(safe(nil, function() return inner:GetSelectedIndex(0) end)),
                    cells = box_children(inner, 1),
                }
            else
                local cell = deck_card_cell(child) or {
                    class = text(safe(nil, function() return child:GetClass():GetFName():ToString() end)),
                }
                cell.position = index
                cell.cursor = widget_shown(member(child, "CardCursor"))
                children[#children + 1] = cell
            end
        end
    end
    return children
end

function demand.deck_census_paths(key, kind, class_name, limit)
    if demand.census.key ~= key then
        demand.census = {key = key, cells = {}, boxes = {}}
    end
    local paths = demand.census[kind]
    local stale = not demand.census[kind .. "_ready"]
    for _, path in ipairs(paths) do
        if refs.resolve(path) == nil then stale = true break end
    end
    if stale then
        paths = {}
        local found = safe(nil, function() return FindAllOf(class_name) end)
        for _, object in ipairs(type(found) == "table" and found or {}) do
            local path = refs.path(object)
            if path and (kind == "cells" or path:find("Card", 1, true))
                and not path:find("Default__", 1, true) then
                paths[#paths + 1] = path
                if #paths >= limit then break end
            end
        end
        demand.census[kind] = paths
        demand.census[kind .. "_ready"] = true
    end
    return paths
end

local function encode_deck_census()
    local top = deck_screen_top(true)
    local key = tostring(refs.path(top)) .. ":" .. tostring(widget_shown(member(top, "Card_EditListNew")))
    local cells = {}
    for _, cached_path in ipairs(demand.deck_census_paths(key, "cells", "MainMenu_Card_Card_Full_C", DECK_CENSUS_MAX_CELLS)) do
        local widget = refs.resolve(cached_path)
        if #cells >= DECK_CENSUS_MAX_CELLS then break end
        if valid(widget) then
            local path = census_path(widget)
            if not path:find("Default__", 1, true) then
                local cell = deck_card_cell(widget) or {}
                cell.path = path
                cell.cursor = widget_shown(member(widget, "CardCursor"))
                cells[#cells + 1] = cell
            end
        end
    end
    local boxes = {}
    for _, cached_path in ipairs(demand.deck_census_paths(key, "boxes", "EndVirtualScrollBox", DECK_CENSUS_MAX_BOXES)) do
        local box = refs.resolve(cached_path)
        if #boxes >= DECK_CENSUS_MAX_BOXES then break end
        if valid(box) then
            local path = census_path(box)
            if path:find("Card", 1, true) and not path:find("Default__", 1, true) then
                local entry = {
                    path = path,
                    shown = widget_shown(box),
                    item_count = integer(safe(nil, function() return box.ItemCount end)),
                    selected = integer(safe(nil, function() return box:GetSelectedIndex(0) end)),
                }
                if (entry.item_count or 0) > 0 and entry.shown and not path:find(":WidgetTree", 1, true) then
                    entry.children = box_children(box, 0)
                end
                if (entry.item_count or 0) > 0 then
                    local content_class = member(box, "ContentClass")
                    entry.content_class = content_class
                        and text(safe(nil, function() return content_class:GetFName():ToString() end)) or nil
                    local contents = reflected_array_items(select(1, read_field(box, {"ContentWidgets"})), 32)
                    local cached = reflected_array_items(select(1, read_field(box, {"CachedWidgets"})), 32)
                    entry.content_count = #contents
                    entry.cached_count = #cached
                    local sample = (contents[1] and contents[1].object) or (cached[1] and cached[1].object)
                    if valid(sample) then
                        entry.sample_class = text(safe(nil, function() return sample:GetClass():GetFName():ToString() end))
                        entry.sample_path = census_path(sample)
                        local names = {}
                        safe(nil, function()
                            sample:GetClass():ForEachProperty(function(property)
                                if #names < 48 then
                                    names[#names + 1] = text(safe(nil, function() return property:GetFName():ToString() end))
                                end
                            end)
                        end)
                        entry.sample_properties = names
                    end
                end
                boxes[#boxes + 1] = entry
            end
        end
    end
    return {cells = cells, boxes = boxes}
end

-- Field dialogue choice wheel (TalkMenu_00_C, a UEndMenuDirectionalSelector:
-- the "Challenge to a match?" Yes/No radial). With lag, a confirm that lands
-- after the dialogue has moved on picks the wheel's default (No after a win)
-- or opens the field menu, so the harness only confirms while this reports
-- the wheel shown. Answer cells carry no text; their layout places them
-- around the wheel and Cursor_00 rotates toward the highlighted answer.
local TALK_MENU_REFRESH_SECONDS = 5
local talk_menu_cache = {menu = nil, scanned_at = nil}

function demand.talk_menu_shown(menu)
    if not valid(menu) then return nil end
    -- The radial keeps Visibility=SelfHitTestInvisible after being detached.
    -- IsInViewport (UMG) stays false even on screen: EndUserWidget's extended
    -- viewport is the live attachment signal used by this game.
    local attached = safe(nil, function() return menu:IsInViewportEx() end)
    if type(attached) ~= 'boolean' then return nil end
    return attached and widget_shown(menu) == true
end

local function find_talk_menu()
    local cached = refs.resolve(talk_menu_cache.menu)
    -- Hidden Talk widgets can remain valid after an NPC conversation. Never
    -- let that stale object mask a newer visible challenge wheel.
    if cached ~= nil and demand.talk_menu_shown(cached) == true then return cached end
    local now = os.time()
    if talk_menu_cache.scanned_at ~= nil
        and now - talk_menu_cache.scanned_at < TALK_MENU_REFRESH_SECONDS then
        return cached
    end
    talk_menu_cache.scanned_at = now
    local found = safe(nil, function() return FindAllOf("TalkMenu_00_C") end)
    local fallback = cached
    for _, menu in ipairs(type(found) == "table" and found or {}) do
        local path = valid(menu) and census_path(menu) or nil
        if path ~= nil and not path:find("Default__", 1, true) then
            if demand.talk_menu_shown(menu) == true then
                talk_menu_cache.menu = refs.path(menu)
                return menu
            end
            if fallback == nil then fallback = menu end
        end
    end
    talk_menu_cache.menu = fallback and refs.path(fallback) or nil
    return fallback
end

local function widget_layout(widget)
    if widget == nil then return nil end
    local transform = safe(nil, function() return widget.RenderTransform end)
    local slot = member(widget, "Slot")
    local offsets = slot and safe(nil, function() return slot.LayoutData.Offsets end) or nil
    return {
        x = transform and number(safe(nil, function() return transform.Translation.X end)) or nil,
        y = transform and number(safe(nil, function() return transform.Translation.Y end)) or nil,
        angle = transform and number(safe(nil, function() return transform.Angle end)) or nil,
        left = offsets and number(safe(nil, function() return offsets.Left end)) or nil,
        top = offsets and number(safe(nil, function() return offsets.Top end)) or nil,
    }
end

local function encode_talk_menu()
    local menu = find_talk_menu()
    if menu == nil then return {observed = false} end
    local answers = {}
    for index = 1, 6 do
        local answer = member(menu, "Answer" .. index)
        if answer ~= nil then
            local entry = widget_layout(answer) or {}
            entry.index = index
            entry.shown = widget_shown(answer)
            answers[#answers + 1] = entry
        end
    end
    local cursor = member(menu, "Cursor_00")
    return {
        observed = true,
        shown = demand.talk_menu_shown(menu),
        in_viewport_ex = safe(nil, function() return menu:IsInViewportEx() end),
        answers = answers,
        cursor_shown = widget_shown(cursor),
        cursor = widget_layout(cursor),
    }
end

-- The deck editor (UEndNewCardDeckWindow behind Card_EditListNew) fills its
-- grids from recycled virtual-list cells that are not reachable in the widget
-- tree, so cells are recorded as the game sets them up: the setup callback
-- hands over (row, widget, index) and the widget carries the card art and its
-- available/owned count at that moment. Selection callbacks give the cursor.
-- Keys are "row:index"; each grid is cleared when its row 0 is set up again
-- from index 0 (a refresh, e.g. after a filter change).
local deck_editor = {grids = {}, cursors = {}, filters = {}, hooks = {}, units = {}, rows = {}}
-- Live, the window's cell-setup callbacks never fired (called natively);
-- the row lists' own OnSetupItem is reached through the scroll box's dynamic
-- delegate, and the window's row setup says which row list is which row.
local DECK_EDITOR_MAX_UNITS = 40
local DECK_EDITOR_MAX_CELLS = 200

local function editor_cell_setup(grid, row, widget, index)
    if row == nil or index == nil then return end
    local cells = deck_editor.grids[grid]
    if cells == nil or (row == 0 and index == 0) then
        cells = {count = 0, cells = {}}
        deck_editor.grids[grid] = cells
    end
    if cells.count >= DECK_EDITOR_MAX_CELLS then return end
    local cell = deck_card_cell(widget) or {}
    cell.row = row
    cell.index = index
    local key = tostring(row) .. ":" .. tostring(index)
    if cells.cells[key] == nil then cells.count = cells.count + 1 end
    cells.cells[key] = cell
end

local function register_editor_hooks()
    if type(RegisterHook) ~= "function" then return end
    local base = "/Script/EndGame.EndNewCardDeckWindow:"
    local function hook(name, callback)
        if deck_editor.hooks[name] ~= nil then return end
        local ok = pcall(function() RegisterHook(base .. name, callback) end)
        deck_editor.hooks[name] = ok
    end
    local function arg(value) return safe(nil, function() return value:get() end) end
    for _, grid in ipairs({"Edit", "All", "Player"}) do
        hook("OnCardList" .. grid .. "CellSetupItem", function(_, row, widget, index)
            if deck_editor.enumerating then return end
            editor_cell_setup(grid, integer(arg(row)), arg(widget), integer(arg(index)))
        end)
        hook("OnCardList" .. grid .. "CellSelectedIndexChanged", function(context, row, index)
            deck_editor.event_sequence = (deck_editor.event_sequence or 0) + 1
            deck_editor.cursors[grid] = {row = integer(arg(row)), index = integer(arg(index)),
                sequence = deck_editor.event_sequence}
            -- Owner discovery must never discard the cursor event deck edits await.
            safe(nil, function()
                local owner = arg(context)
                if valid(owner) then
                    local top = deck_screen_top()
                    deck_editor.owner, deck_editor.owner_top = refs.path(owner), refs.path(top)
                end
            end)
        end)
        hook("OnCardList" .. grid .. "RowSelectedIndexChanged", function(_, row)
            local cursor = deck_editor.cursors[grid] or {}
            cursor.row = integer(arg(row))
            deck_editor.cursors[grid] = cursor
        end)
    end
    -- Once only, like the hooks above: registered on every snapshot it
    -- piled up callbacks while a deck screen was open until the game crashed
    -- (live 2026-09-29: hook ids 451..460 inside two seconds, then a crash).
    if deck_editor.hooks["EndCardUnitList:OnSetupItem"] == nil then
        local unit_ok = pcall(function()
            RegisterHook("/Script/EndGame.EndCardUnitList:OnSetupItem", function(unit, widget, index)
                local list = safe(nil, function() return unit:get() end)
                local key = list and census_path(list) or nil
                local cell_index = integer(arg(index))
                if key == nil or cell_index == nil then return end
                local cells = deck_editor.units[key]
                if cells == nil then
                    local count = 0
                    for _ in pairs(deck_editor.units) do count = count + 1 end
                    if count >= DECK_EDITOR_MAX_UNITS then return end
                    cells = {}
                    deck_editor.units[key] = cells
                end
                local cell = deck_card_cell(arg(widget)) or {}
                cell.index = cell_index
                cells[cell_index] = cell
            end)
        end)
        deck_editor.hooks["EndCardUnitList:OnSetupItem"] = unit_ok
    end
    for _, grid in ipairs({"Edit", "All", "Player"}) do
        hook("OnCardList" .. grid .. "RowSetupItem", function(_, widget, index)
            local row_widget = arg(widget)
            local row = integer(arg(index))
            if row == nil or not valid(row_widget) then return end
            deck_editor.rows[grid] = deck_editor.rows[grid] or {}
            deck_editor.rows[grid][row] = census_path(row_widget)
        end)
    end
    for filter = 1, 7 do
        local name = string.format("OnListBoxFilter%02dPressedItem", filter)
        hook(name, function(_, index)
            deck_editor.filters[#deck_editor.filters + 1] = {group = filter, index = integer(arg(index)),
                root = census_path(deck_screen_top())}
            if #deck_editor.filters > 32 then table.remove(deck_editor.filters, 1) end
        end)
    end
    hook("OnEditingDeckSelectedIndexChanged", function(_, index)
        deck_editor.cursors.deck_slot = {index = integer(arg(index))}
    end)
    -- The deck strip above the owned grid (Up from grid row 0). Live
    -- 2026-09-26 its cells show Delete, so pressing Confirm there removes a
    -- card: its cursor and presses are recorded. Every cursor event gets a
    -- running sequence so the area moved last (grid or strip) is known.
    hook("OnCardDeckListSelectedIndexChanged", function(_, index)
        deck_editor.event_sequence = (deck_editor.event_sequence or 0) + 1
        deck_editor.cursors.Strip = {index = integer(arg(index)), sequence = deck_editor.event_sequence}
    end)
    hook("OnCardDeckListPressedItem", function(_, index)
        deck_editor.event_sequence = (deck_editor.event_sequence or 0) + 1
        deck_editor.strip_presses = deck_editor.strip_presses or {}
        table.insert(deck_editor.strip_presses, {index = integer(arg(index)), sequence = deck_editor.event_sequence})
        if #deck_editor.strip_presses > 16 then table.remove(deck_editor.strip_presses, 1) end
    end)
end

local function encode_deck_editor()
    local grids = {}
    for grid, data in pairs(deck_editor.grids) do
        local cells = {}
        for _, cell in pairs(data.cells) do cells[#cells + 1] = cell end
        table.sort(cells, function(a, b)
            if a.row ~= b.row then return a.row < b.row end
            return a.index < b.index
        end)
        grids[grid] = cells
    end
    -- Rows known from the row-setup callbacks, with cells from each row
    -- list's own setup callback.
    for grid, rows in pairs(deck_editor.rows) do
        local cells = grids[grid] or {}
        for row, unit_path in pairs(rows) do
            for _, cell in pairs(deck_editor.units[unit_path] or {}) do
                local copy = {}
                for key, value in pairs(cell) do copy[key] = value end
                copy.row = row
                cells[#cells + 1] = copy
            end
        end
        table.sort(cells, function(a, b)
            if a.row ~= b.row then return a.row < b.row end
            return a.index < b.index
        end)
        grids[grid] = cells
    end
    local unit_count = 0
    for _ in pairs(deck_editor.units) do unit_count = unit_count + 1 end
    return {grids = grids, cursors = deck_editor.cursors, filters = deck_editor.filters,
        hooks = deck_editor.hooks, unit_lists = unit_count, strip_presses = deck_editor.strip_presses}
end

-- Every card on the deck screens is drawn by an EndBillboard whose image
-- path names the card art (as for the hand). The editor's grid cells are not
-- reachable through the list widgets or their callbacks, so the billboards
-- themselves are collected: art (identity via the catalogue) plus object
-- path, whose outer chain names the list, row and cell. Throttled and bounded,
-- and only while the deck screen is up.
local DECK_BILLBOARD_PERIOD_SECONDS = 3
local DECK_BILLBOARD_MAX = 160
local deck_billboard_cache = {at = nil, items = {}}

local function encode_deck_billboards()
    local now = os.time()
    if deck_billboard_cache.at ~= nil and now - deck_billboard_cache.at < DECK_BILLBOARD_PERIOD_SECONDS then
        return deck_billboard_cache.items
    end
    deck_billboard_cache.at = now
    local items = {}
    local found = safe(nil, function() return FindAllOf("EndBillboard") end)
    for _, billboard in ipairs(type(found) == "table" and found or {}) do
        if #items >= DECK_BILLBOARD_MAX then break end
        if valid(billboard) then
            local asset = text(safe(nil, function() return billboard.ImagePath.AssetPathName end))
            if asset ~= nil and asset:find("CardGame_Card", 1, true) then
                local path = census_path(billboard)
                if path ~= nil and not path:find("Default__", 1, true) and path:find("MainMenu_Card", 1, true) then
                    local identity = card_identity_from_asset_path(asset)
                    local entry = catalogue_entry_for_identity(identity)
                    items[#items + 1] = {
                        path = path,
                        card_id = identity and identity.card_id or nil,
                        name = entry and text(entry.name) or nil,
                        shown = widget_shown(billboard),
                    }
                end
            end
        end
    end
    deck_billboard_cache.items = items
    return items
end

-- The editor's grid cells are drawn natively (no widget, billboard or
-- callback exposes them), but its Toggle Details (F4) popup follows the grid
-- cursor and is an ordinary MainMenu_Card_CardInformation_C whose text blocks
-- read e.g. "Card #7" / "Levrikon". Text blocks under CardInformation widgets
-- are collected (leaf name, text, shown); a shown text naming a catalogue
-- card identifies the hovered card. Throttled and deck-screen only.
-- Live 2026-09-26 the popup's Txt_Num_01 holds the card number, which is
-- CardGameCardInfo.UniqueId ("Card #111" = Magic Pot), and Txt_Name holds the
-- name localisation key (not unique), so identity is resolved by number.
local card_catalogue_by_unique_id = {}
do
    local function index(entry)
        if type(entry) ~= "table" then return end
        local unique = integer(entry.unique_id)
        if unique ~= nil and card_catalogue_by_unique_id[unique] == nil then
            card_catalogue_by_unique_id[unique] = entry
        end
    end
    for key, entry in pairs(card_catalogue) do
        if type(key) == "string" and key:match("^Card_%d%d%d$") then index(entry) end
    end
    for _, entry in pairs(auxiliary_card_catalogue) do index(entry) end
end

local DECK_DETAIL_MAX = 240
-- Only the fields that identify a card are kept (live, the whole-screen text
-- scan overflowed the cap before reaching the editor's popups).
local DECK_DETAIL_FIELDS = {Txt_Num_01 = true, Txt_Name = true, Txt_Info = true}
-- Finding the popup's text blocks walks every EndTextBlock in the game
-- (live 2026-09-29 the probe averaged 166 ms a call, half of them over a
-- frame, and throttled with os.time it refreshed at most once a second, the
-- floor on reading a deck). The blocks are found at most every
-- DECK_DETAIL_DISCOVERY_SECONDS (or when one has gone) and only they are
-- re-read only on an explicit request. Cursor acknowledgements never discover
-- new blocks; full screen reads discover missing/replaced popup paths.
local DECK_DETAIL_DISCOVERY_SECONDS = 3
local deck_detail_clock = (os and os.clock) or function() return os.time() end
local deck_detail_cache = {at = nil, value = nil, discovered_at = nil, blocks = nil}

local function discover_deck_detail_blocks()
    local blocks = {}
    local totals = 0
    local found = safe(nil, function() return FindAllOf("EndTextBlock") end)
    for _, block in ipairs(type(found) == "table" and found or {}) do
        if #blocks >= DECK_DETAIL_MAX then break end
        if valid(block) then
            local path = census_path(block) or ""
            -- Deck totals ("14/15"): live 2026-09-26 the deck-select list's total
            -- does not change while the editor is open, whose header has its own.
            if totals < 12 and path:find("MainMenu_Card", 1, true) and not path:find(":WidgetTree", 1, true) then
                local value = text(safe(nil, function() return block.Text end))
                -- A short deck is drawn red: "<color=red>14</color>/15".
                local plain = value and value:gsub("<[^>]*>", "") or nil
                if plain ~= nil and plain:match("^%s*%d+%s*/%s*%d+%s*$") then
                    blocks[#blocks + 1] = {path = refs.path(block), kind = "total"}
                    totals = totals + 1
                end
            end
            -- Object paths use member names ("...WidgetTree.Information..."),
            -- not the CardInformation class name.
            if path:find("Information", 1, true) and not path:find("Default__", 1, true)
                and not path:find(":WidgetTree", 1, true) then
                local parent, leaf = path:match("^(.*)%.([^.]+)$")
                if parent ~= nil and leaf ~= nil and DECK_DETAIL_FIELDS[leaf] then
                    -- Prime the bounded native identity cache while this
                    -- discovery wrapper is fresh. census_path is diagnostic
                    -- text only; it cannot seed later safe path resolutions.
                    local item = {path = refs.path(block), kind = "field", parent = parent, leaf = leaf}
                    if leaf == "Txt_Num_01" then
                        -- text block -> WidgetTree -> popup widget
                        local tree = safe(nil, function() return block:GetOuter() end)
                        local popup = valid(tree) and safe(nil, function() return tree:GetOuter() end) or nil
                        item.root = refs.path(popup)
                    end
                    blocks[#blocks + 1] = item
                end
            end
        end
    end
    return blocks
end

local function encode_deck_detail(cached_only)
    local now = deck_detail_clock()
    deck_detail_cache.at = now
    local blocks = deck_detail_cache.blocks
    local area, newest = nil, -1
    for name, cursor in pairs(deck_editor.cursors) do
        if (cursor.sequence or -1) > newest then
            area, newest = name, cursor.sequence
        end
    end
    local stale = blocks == nil or deck_detail_cache.discovered_at == nil
        or now - deck_detail_cache.discovered_at >= DECK_DETAIL_DISCOVERY_SECONDS
        or area ~= deck_detail_cache.area
    if not stale then
        for _, item in ipairs(blocks) do
            if refs.resolve(item.path) == nil or (item.root and refs.resolve(item.root) == nil) then
                stale = true break
            end
        end
    end
    if stale and not cached_only then
        blocks = discover_deck_detail_blocks()
        deck_detail_cache.blocks = blocks
        deck_detail_cache.discovered_at = now
        deck_detail_cache.area = area
    end
    local texts = {}
    local groups = {}
    local totals = {}
    for _, item in ipairs(blocks or {}) do
        local block = refs.resolve(item.path)
        local value = text(safe(nil, function() return block.Text end))
        if item.kind == "total" then
            totals[#totals + 1] = {text = value, shown = widget_shown(block), path = item.path}
        else
            local entry = {name = item.leaf, text = value, shown = widget_shown(block)}
            texts[#texts + 1] = entry
            groups[item.parent] = groups[item.parent] or {}
            groups[item.parent][item.leaf] = entry
            if item.root ~= nil then groups[item.parent].__root = refs.resolve(item.root) end
        end
    end
    -- Every popup that names a card is reported; the hovered card is the one
    -- not in the opponent panel (EnemyInformation.CardInfo always shows the
    -- opponent's featured card).
    -- The grid and the deck strip each have a popup of the same class; only
    -- the one over the focused area is visible, so each popup root's own
    -- visibility decides which card is hovered.
    local hovered = nil
    local resolved = {}
    for parent, fields in pairs(groups) do
        local number = fields.Txt_Num_01 and fields.Txt_Num_01.shown and integer(fields.Txt_Num_01.text) or nil
        local entry = number and card_catalogue_by_unique_id[number] or nil
        if entry ~= nil then
            local root = fields.__root
            if root == nil then
                local root_path = parent:gsub("%.WidgetTree$", "")
                root = safe(nil, function() return StaticFindObject(root_path) end)
            end
            local card = {
                card_id = text(entry.card_id),
                name = text(entry.name),
                unique_id = number,
                name_id = fields.Txt_Name and fields.Txt_Name.text or nil,
                popup = parent:match("([^.]+)%.WidgetTree$") or parent,
                path = parent,
                popup_shown = valid(root) and widget_shown(root) or nil,
            }
            resolved[#resolved + 1] = card
            if not parent:find("EnemyInformation", 1, true) then
                if card.popup_shown == true and (hovered == nil or hovered.popup_shown ~= true) then
                    hovered = card
                elseif hovered == nil then
                    hovered = card
                end
            end
        end
    end
    deck_detail_cache.value = {hovered = hovered, resolved = resolved, texts = texts, totals = totals}
    return deck_detail_cache.value
end

local function encode_deck_screen(cached_only)
    local top = deck_screen_top(cached_only)
    if top == nil then return {observed = false} end
    pcall(register_deck_hooks)
    pcall(register_editor_hooks)
    local windows = {}
    for _, name in ipairs({"DeckList", "CardDeckList", "CardList_Player", "Card_ListAll",
        "Card_EditListNew", "Card_EditList", "EnemyInformation", "SortWindow", "PresetTop", "DeckCopy"}) do
        windows[name] = widget_shown(member(top, name))
    end
    return {
        observed = true,
        shown = widget_shown(top),
        windows = windows,
        deck = deck_list_window(member(top, "CardList_Player"), "Virtual_Scroll_Box_Card"),
        collection = deck_list_window(member(top, "Card_ListAll"), "Virtual_Scroll_Box_Card"),
        enemy = deck_list_window(member(top, "EnemyInformation"), "Virtual_Scroll_Box_Card"),
        edit = (function()
            local edit = member(top, "Card_EditListNew")
            if edit == nil then return nil end
            return {
                shown = widget_shown(edit),
                description = member_text(edit, "Txt_Description"),
                list = deck_scroll_box(member(edit, "Virtual_Scroll_Box_Edit"), 0),
            }
        end)(),
        events = deck_screen_events,
        census = not cached_only and encode_deck_census() or nil,
        editor = encode_deck_editor(),
        billboards = not cached_only and encode_deck_billboards() or nil,
        detail = encode_deck_detail(cached_only),
    }
end


-- CardGame_Test_C authors CardFrameCursor_01..16 (15 cells + the placement
-- slot). Their visibility is exported raw so the placement cursor can be
-- located on cells the held card cannot use (the preview only marks lit
-- cells); a single shown cursor among the 15 cells is reported as the cell.
local function board_frame_cursors(root, root_path)
    local raw = {}
    local shown = {}
    for index = 0, BOARD_CELLS - 1 do
        local widget = root ~= nil and refs.resolve(board_cursor_cache[index]) or nil
        if not valid(widget) and root ~= nil then
            local name = string.format("CardFrameCursor_%02d", index + 1)
            widget = safe(nil, function() return root[name] end)
            if not valid(widget) and root_path ~= nil then
                widget = safe(nil, function() return StaticFindObject(root_path .. ".WidgetTree." .. name) end)
            end
            board_cursor_cache[index] = refs.path(widget)
        end
        local visibility = valid(widget) and integer(safe(nil, function() return widget.Visibility end)) or nil
        raw[index + 1] = visibility == nil and -1 or visibility
        if visibility ~= nil and SHOWN_VISIBILITY[visibility] then shown[#shown + 1] = index end
    end
    return raw, (#shown == 1) and shown[1] or nil
end

-- World location of a mini-game actor. The card game renders in its own
-- world from the fixed 0000-COMON_CardGame_Gimmick layout, so cell positions
-- are the same in every match: placed cards (CardBoardActors) sit on their
-- cells and the held card (CardPlacementActor) hovers over the cursor cell,
-- including cells the card cannot use, which the preview never marks.
local function actor_location(actor)
    if not valid(actor) then return nil end
    local vector = safe(nil, function() return actor:K2_GetActorLocation() end)
    if vector == nil then return nil end
    local x = number(safe(nil, function() return vector.X end))
    local y = number(safe(nil, function() return vector.Y end))
    local z = number(safe(nil, function() return vector.Z end))
    if x == nil or y == nil or z == nil then return nil end
    return {x = x, y = y, z = z}
end

-- Compare only freshly resolved actors; retain primitive identities, never
-- UObject wrappers or dereference an address retained across a pause.
function demand.board_identity(actor)
    if not valid(actor) then return nil end
    local name = text(safe(nil, function() return actor:GetFullName() end))
    local address = safe(nil, function() return string.format("%x", actor:GetAddress()) end)
    if name == nil or address == nil then return nil end
    return name .. "@" .. address
end

local function encode_match_board(manager)
    local root, root_path = live_test_root()
    local manager_name = text(safe(nil, function() return manager:GetFullName() end))
    local manager_identity = demand.board_identity(manager)
    if manager_name ~= board_owner_manager then
        board_flag_cache = {}
        board_card_owner = {}
        board_owner_manager = manager_name
    end
    demand.board_owner_identity = manager_identity
    local infos = reflected_array_items(select(1, read_field(manager, {"FieldInfos"})), BOARD_CELLS)
    local actors = reflected_array_items(select(1, read_field(manager, {"CardBoardActors"})), BOARD_CELLS)
    local placement_actor = select(1, read_field(manager, {"CardPlacementActor"}))
    local placing = valid(placement_actor)
    local live = {}
    for _, entry in ipairs(infos) do
        local index = integer(entry.index)
        local struct = entry.object
        if index ~= nil and index >= 0 and index < BOARD_CELLS and struct ~= nil then
            live[index] = {
                players = array_length(safe(nil, function() return struct.FlagPlayers end)) or 0,
                opponents = array_length(safe(nil, function() return struct.FlagOpponents end)) or 0,
                highlight = boolean(safe(nil, function() return struct.bCursorVisible end)) == true,
            }
        end
    end
    local occupant = {}
    local occupant_actor = {}
    for _, entry in ipairs(actors) do
        local index = integer(entry.index)
        if index ~= nil and index >= 0 and index < BOARD_CELLS and valid(entry.object) then
            occupant_actor[index] = entry.object
            occupant[index] = text(safe(nil, function() return entry.object:GetFullName() end)) or true
        end
    end
    local complete = #infos == BOARD_CELLS
    if complete and demand.suspended_board ~= nil then
        local suspended = demand.suspended_board
        if manager_identity ~= nil and manager_identity == suspended.manager then
            for index = 0, BOARD_CELLS - 1 do
                local record = suspended.cards[index]
                if record ~= nil and record.identity ~= nil
                    and record.identity == demand.board_identity(occupant_actor[index]) then
                    board_card_owner[index] = record
                end
            end
        end
        demand.suspended_board = nil
    end
    if complete and not placing then
        for index = 0, BOARD_CELLS - 1 do
            local card = occupant[index]
            local record = board_card_owner[index]
            if card == nil then
                board_card_owner[index] = nil
            elseif record == nil or record.card ~= card then
                -- A card may only be placed on its owner's pawn cell (or on its
                -- owner's card), so the pre-placement owner of the cell is the
                -- card's owner. Cards never change sides once placed.
                local before = board_flag_cache[index]
                local owner = "unknown"
                if record ~= nil and record.owner ~= "unknown" then
                    owner = record.owner
                elseif before ~= nil and before.players > 0 then
                    owner = "player"
                elseif before ~= nil and before.opponents > 0 then
                    owner = "opponent"
                end
                board_card_owner[index] = {card = card, owner = owner}
            end
            if board_card_owner[index] ~= nil then
                board_card_owner[index].identity = demand.board_identity(occupant_actor[index])
            end
            board_flag_cache[index] = {players = live[index].players, opponents = live[index].opponents}
        end
    end
    local cells = {}
    local target = nil
    for index = 0, BOARD_CELLS - 1 do
        local now = live[index] or {players = 0, opponents = 0, highlight = false}
        local flags = placing and board_flag_cache[index] or now
        flags = flags or {players = 0, opponents = 0}
        local card = occupant[index]
        local record = board_card_owner[index]
        local owner = "empty"
        if card ~= nil then
            owner = record and record.owner or "unknown"
        elseif flags.players > 0 then
            owner = "player"
        elseif flags.opponents > 0 then
            owner = "opponent"
        end
        if placing and now.highlight and card == nil
            and now.players + now.opponents == 0
            and flags.players + flags.opponents > 0 then
            target = index
        end
        local cell = {
            row = math.floor(index / 5),
            column = index % 5,
            owner = owner,
            rank = card == nil and math.max(flags.players, flags.opponents) or 0,
            card_present = card ~= nil,
            highlight = now.highlight,
        }
        if card ~= nil then
            local identity, power = nil, nil
            if root ~= nil then identity, power = board_card_face(index, root, root_path) end
            local catalogue_entry = catalogue_entry_for_identity(identity)
            cell.card_id = identity and identity.card_id or nil
            cell.name = catalogue_entry and text(catalogue_entry.name) or nil
            cell.power = power
            cell.base_power = catalogue_entry and integer(catalogue_entry.power) or nil
            cell.location = actor_location(occupant_actor[index])
        end
        cells[#cells + 1] = cell
    end
    local lanes = {}
    local player_lanes = reflected_array_items(select(1, read_field(manager, {"TokenPlayerCounts"})), 3)
    local opponent_lanes = reflected_array_items(select(1, read_field(manager, {"TokenEnemyCounts"})), 3)
    for lane = 1, 3 do
        lanes[lane] = {
            row = lane - 1,
            player_power = player_lanes[lane] and integer(player_lanes[lane].object) or nil,
            opponent_power = opponent_lanes[lane] and integer(opponent_lanes[lane].object) or nil,
        }
    end
    return {
        rows = 3,
        columns = 5,
        cells = complete and cells or {},
        lanes = lanes,
        placement = (function()
            local raw, cursor = board_frame_cursors(root, root_path)
            return {
                active = placing,
                target_row = target and math.floor(target / 5) or nil,
                target_column = target and target % 5 or nil,
                cursor_row = cursor and math.floor(cursor / 5) or nil,
                cursor_column = cursor and cursor % 5 or nil,
                actor_location = placing and actor_location(placement_actor) or nil,
                frame_cursor_visibility = raw,
            }
        end)(),
        source = "EndCardGame3DManager.FieldInfos+CardBoardActors",
    }
end

-- A match is in progress once the manager shows a dealt hand, a turn
-- banner, or a card on the board. Before that (deck select, Begin Match
-- popup) the card UI branch must keep publishing begin_match_available.
local function manager_match_in_progress(manager)
    if boolean(select(1, read_field(manager, {"bPlayerTurnVisible"}))) == true
        or boolean(select(1, read_field(manager, {"bEnemyTurnVisible"}))) == true then
        return true
    end
    local raw_hand = select(1, read_field(manager, {"CardHandActors"}))
    for _, entry in ipairs(reflected_array_items(raw_hand, CARD_WIDGET_POOL_SIZE)) do
        if valid(entry.object) then
            local card_index = integer(select(1, read_field(entry.object, {"CardIndex"})))
            if card_index ~= nil and card_index >= 0 then return true end
        end
    end
    local raw_board = select(1, read_field(manager, {"CardBoardActors"}))
    for _, entry in ipairs(reflected_array_items(raw_board, 15)) do
        if valid(entry.object) then return true end
    end
    return false
end

local function refresh_cursor_from_hand_actors()
    local actors = cached_hand_actors
    if actors == nil then return end
    local cursor_slot = nil
    local cursor_true_count = 0
    local cursor_observed = false
    for _, entry in ipairs(actors) do
        local object = refs.resolve(entry.path)
        if object == nil then
            cached_hand_actors = nil
            return
        end
        local cursor_visible = boolean(select(1, read_field(object, {"bCursorVisible"})))
        if cursor_visible ~= nil then
            cursor_observed = true
            if cursor_visible and entry.index >= 0 and entry.index < CARD_WIDGET_POOL_SIZE then
                cursor_true_count = cursor_true_count + 1
                cursor_slot = entry.index
            end
        end
    end
    if cursor_observed and cursor_true_count == 1 and cursor_slot ~= nil then
        live_card_focus_slot = cursor_slot
        live_card_focus_source = "manager.CardHandActors[].bCursorVisible"
    elseif cursor_observed then
        live_card_focus_slot = nil
        live_card_focus_source = nil
    end
end

-- Per-section game-thread cost, published with the call totals so a slow
-- snapshot can be traced to its part (live 2026-09-30 calls averaged 116 ms
-- mid-match with a 1.2 s worst case).
local section_clock = (os and os.clock) or function() return 0 end
local section_timing = {}

local function timed(name, fn, ...)
    local started = section_clock()
    local results = table.pack(fn(...))
    local elapsed = (section_clock() - started) * 1000
    local entry = section_timing[name]
    if entry == nil then
        entry = {calls = 0, total_ms = 0, max_ms = 0}
        section_timing[name] = entry
    end
    entry.calls = entry.calls + 1
    entry.total_ms = entry.total_ms + elapsed
    if elapsed > entry.max_ms then entry.max_ms = elapsed end
    return table.unpack(results, 1, results.n)
end

function demand.encode_deck_select_snapshot(cached_only)
    return {
                schema_version = 1,
                active = false,
                phase = "inactive",
                turn = 0,
                match_id = nil,
                screen = "deck_select",
                begin_match_available = true,
                popups = timed("encode_popups", encode_popups),
                deck_screen = timed("encode_deck_screen", encode_deck_screen, cached_only),
                match_screen = true,
                talk_menu = {observed = false},
                cursor = {area = "unknown"},
                hand = {},
                legal_moves = {},
                board = {rows = 3, columns = 5, cells = {}},
                reflection = {
                    ui_class = "deck_screen_top",
                    manager_found = false,
                    hand_observed = false,
                    legal_moves_observed = false,
                    board_observed = false,
                },
            }, nil
end

local function encode_snapshot(sequence)
    if not sequence then return nil, "state_sequence_unavailable" end
    -- A positively visible, already discovered deck root cannot be a match
    -- manager. Avoid whole-array UI/manager searches until this root closes.
    if deck_screen_top(true) ~= nil then
        return demand.encode_deck_select_snapshot(false)
    end
    write_debug(GAME_DEBUG_PATH, "before_manager", sequence)
    -- The runtime object path was confirmed during discovery.  Use a bounded
    -- exact lookup here: FindFirstOf(class) can stall the game thread while
    -- the card UI owns the transition.
    local manager_class = "EndCardGame3DManager"
    local manager = timed("find_manager", find_manager)
    write_debug(GAME_DEBUG_PATH, "after_manager", manager ~= nil)
    if not valid(manager) then manager = nil end
    write_debug(GAME_DEBUG_PATH, "after_manager_valid", manager ~= nil)
    -- Once a match is under way the manager publishes everything, and the
    -- card-UI root lookup was 68% of the probe's game-thread time over a live
    -- match (82 ms a call, 2026-09-30), so it only runs before the match.
    local in_match = manager ~= nil
        and timed("manager_match_in_progress", manager_match_in_progress, manager) == true
    local ui, ui_class = nil, nil
    if not in_match then
        ui, ui_class = timed("first_ui_object", first_ui_object, UI_CLASSES)
    end
    -- The card UI can publish its five named hand widgets before the 3D match
    -- manager exists. Export those bounded identities on every card-game UI
    -- sample; Python infers the mulligan only after it has sent Begin Match.
    if ui ~= nil and not in_match then
        write_debug(GAME_DEBUG_PATH, "ui_discovery", ui_class or "")
        timed("debug_live_card_image_identity", debug_live_card_image_identity, ui, true)
        timed("refresh_cursor_from_hand_actors", refresh_cursor_from_hand_actors)
        local identity_hand = timed("encode_live_identity_hand", encode_live_identity_hand)
        local direct_mulligan_marker = timed("live_mulligan_marker_observed", live_mulligan_marker_observed)
        return {
            schema_version = 1,
            active = false,
            phase = "inactive",
            turn = 0,
            match_id = nil,
            screen = "deck_select",
            begin_match_available = true,
            popups = timed("encode_popups", encode_popups),
            deck_screen = timed("encode_deck_screen", encode_deck_screen),
            match_screen = true,
            cursor = timed("encode_live_card_cursor", encode_live_card_cursor),
            hand = identity_hand,
            mulligan_selection = timed("encode_mulligan_selection", encode_mulligan_selection),
            legal_moves = {},
            board = {rows = 3, columns = 5, cells = {}},
            player_score = nil,
            opponent_score = nil,
            winner = nil,
            reflection = {
                manager_class = nil,
                ui_class = ui_class,
                manager_found = false,
                manager_lookup = manager_lookup,
                hand_observed = #identity_hand > 0,
                card_identity_count = timed("live_identity_count", live_identity_count),
                card_probe = live_card_probe,
                mulligan_marker_observed = direct_mulligan_marker,
                hand_field = #identity_hand > 0
                    and "CardGame_Card_Full_C.Billboard_Thumbnail.ImagePath.AssetPathName" or nil,
                hand_source = #identity_hand > 0 and "live_card_widgets" or "manager_not_constructed",
                legal_moves_observed = false,
                board_observed = false,
            },
        }, nil
    end
    -- During normal exploration and during startup there is no card-game
    -- manager.  Do not scan every card/board/move class in that state: those
    -- FindAllOf calls run on the UE4 game thread and can stall boot.
    if manager == nil then
        -- The pre-match deck-select screen is found through its own root
        -- (deck_screen_top) when no card-game UI root is accepted: live
        -- 2026-09-28 the TopNPC root did not report IsInViewport itself, so
        -- the screen was on display with deck_screen unpublished.
        local deck_top = timed("deck_screen_top", deck_screen_top)
        if deck_top ~= nil and widget_shown(deck_top) then
            return demand.encode_deck_select_snapshot(false)
        end
        -- A fully inactive screen is the lifecycle boundary for the next
        -- match. Drop cached slot diagnostics and identities here so a later
        -- hand cannot inherit state from an earlier match.
        last_card_indices = {}
        manager_hand_seen = false
        live_card_identity_by_slot = {}
        live_card_focus_slot = nil
        live_card_focus_source = nil
        cached_hand_actors = nil
        hand_ids.read = nil
        hand_ids.read_at = nil
        last_manager_identity = nil
        return {
            schema_version = 1,
            active = false,
            phase = "inactive",
            turn = 0,
            match_id = nil,
            screen = nil,
            begin_match_available = false,
            match_screen = false,
            talk_menu = timed("encode_talk_menu", encode_talk_menu),
            cursor = {area = "unknown", hand_index = nil, row = nil, column = nil},
            hand = {},
            legal_moves = {},
            board = {rows = 3, columns = 5, cells = {}},
            player_score = nil,
            opponent_score = nil,
            winner = nil,
            reflection = {
                manager_class = nil,
                ui_class = nil,
                manager_found = false,
                manager_lookup = manager_lookup,
                hand_observed = false,
                legal_moves_observed = false,
                board_observed = false,
            },
        }, nil
    end
    local manager_identity = text(safe(nil, function() return manager:GetFullName() end))
    if last_manager_identity ~= nil and manager_identity ~= nil
        and manager_identity ~= last_manager_identity then
        last_card_indices = {}
        live_card_identity_by_slot = {}
        live_card_focus_slot = nil
        live_card_focus_source = nil
        cached_hand_actors = nil
        hand_ids.read = nil
        hand_ids.read_at = nil
        write_debug(GAME_DEBUG_PATH, "card_title_cache", "manager_changed")
    end
    last_manager_identity = manager_identity
    -- Card names are resolved from the offline catalogue join.  The historical
    -- runtime catalogue UObject scan is intentionally not run in production:
    -- it added periodic FindAllOf/property work without contributing to the
    -- published identity or name.
    local turn_shown = boolean(select(1, read_field(manager, {"bPlayerTurnVisible"}))) == true
        or boolean(select(1, read_field(manager, {"bEnemyTurnVisible"}))) == true
    local clock_now = section_clock()
    -- The mulligan (no turn shown yet) swaps cards in place, so identities are
    -- re-read on every sample until play starts.
    if not turn_shown or hand_ids.missing or hand_ids.seen ~= hand_ids.read
        or hand_ids.read_at == nil or clock_now - hand_ids.read_at >= hand_ids.refresh_seconds then
        local before = last_card_image_identity_debug_at
        timed("debug_live_card_image_identity", debug_live_card_image_identity, nil, true)
        if last_card_image_identity_debug_at ~= before then
            hand_ids.read = hand_ids.seen
            hand_ids.read_at = clock_now
        end
    end
    -- Use manager hand actors when available, then fill a partial result from
    -- the five bounded CardFull widgets without inspecting the banner.
    local hand = timed("encode_manager_hand", encode_manager_hand, manager)
    -- The widget-slot hand is only a stand-in while the manager hand is not
    -- yet readable; mid-match hands are routinely shorter than five cards.
    if #hand == 0 then
        local identity_hand = timed("encode_live_identity_hand", encode_live_identity_hand)
        if #identity_hand > 0 then hand = identity_hand end
    end
    if #hand >= 5 then manager_hand_seen = true end
    local mulligan_selection = timed("encode_mulligan_selection", encode_mulligan_selection)
    -- The class/property/function diagnostics above were intentionally used
    -- only to establish the runtime schema.  They are not part of the live
    -- sampling path: repeated generated-class reflection made the game
    -- thread stale during the deck-to-mulligan transition.
    write_debug(GAME_DEBUG_PATH, "manager_hand_count", #hand)
    local active = #hand >= 5 or timed("manager_match_in_progress", manager_match_in_progress, manager)
    -- These are direct AEndCardGame3DManager state flags from the extracted
    -- manager schema, not rendered-widget state. They remain false while the
    -- opening five-card mulligan is displayed and become true for the
    -- corresponding turn indicator once the hand is accepted.
    local player_turn_visible = boolean(select(1, read_field(manager, {"bPlayerTurnVisible"})))
    local enemy_turn_visible = boolean(select(1, read_field(manager, {"bEnemyTurnVisible"})))
    local match_result = timed("encode_match_result", encode_match_result)
    local phase = match_result.shown and "result"
        or player_turn_visible and "player_turn"
        or enemy_turn_visible and "opponent_turn"
        or active and "dealing"
        or "inactive"
    local screen = player_turn_visible and "match"
        or enemy_turn_visible and "match"
        or nil
    local turn = (player_turn_visible or enemy_turn_visible) and 1 or 0
    local begin_match_available = false
    local hand_field = #hand > 0 and "CardHandActors.CardIndex" or nil
    local cursor_field = #hand > 0 and "CardHandActors.bCursorVisible" or nil
    write_debug(GAME_DEBUG_PATH, "after_hand", hand_field)

    return {
        schema_version = 1,
        active = active,
        phase = phase,
        turn = turn,
        match_id = nil,
        cursor = timed("encode_live_card_cursor", encode_live_card_cursor),
        hand = hand,
        mulligan_selection = mulligan_selection,
        legal_moves = {},
        board = timed("encode_match_board", encode_match_board, manager),
        result = match_result,
        popups = timed("encode_popups", encode_popups),
        player_score = nil,
        opponent_score = nil,
        winner = nil,
        screen = screen,
        begin_match_available = begin_match_available,
        match_screen = active,
        reflection = {
                manager_class = manager_class,
                ui_class = ui_class,
                manager_found = manager ~= nil,
                manager_lookup = manager_lookup,
                phase_field = "bPlayerTurnVisible/bEnemyTurnVisible",
                screen_field = nil,
                begin_match_field = nil,
                hand_field = hand_field,
                hand_source = "EndCardGame3DManager.CardHandActors",
                cursor_field = cursor_field,
                cursor_source = cursor_field,
                moves_field = nil,
                board_field = nil,
                hand_observed = #hand > 0,
                card_identity_count = timed("live_identity_count", live_identity_count),
                card_probe = live_card_probe,
                mulligan_selection_observed = mulligan_selection.observed,
                mulligan_selection_source = mulligan_selection.source,
                legal_moves_observed = false,
                board_observed = false,
        },
    }, nil
end

local function write_snapshot(payload)
    local file = io.open(TEMP_PATH, "wb")
    if not file then return false end
    local ok = pcall(function() file:write(json(payload)); file:flush(); file:close() end)
    if not ok then pcall(function() file:close() end); return false end
    os.remove(OUTPUT_PATH)
    return os.rename(TEMP_PATH, OUTPUT_PATH) ~= nil
end

-- The probe only looks for card-game UI while the agent has asked for it.
-- Each UI lookup is a FindAllOf over the whole object array, about 400 ms on
-- this title; run once a second in the open world it cost 392 ms of every
-- second on the game thread and a >250 ms freeze roughly once a second
-- (live 2026-09-28, Cloud standing idle at Costa del Sol). The agent writes
-- queen_blood.request: the lease records verified Yes (or dialogue-only Talk).
-- A unique ticket requests one sample; the lease alone does no discovery.
local REQUEST_PATH = ROOT .. "queen_blood.request"

function demand.read_probe_request()
    local file = io.open(REQUEST_PATH, "rb")
    if not file then return false end
    local content = file:read("*a")
    file:close()
    local fields = {}
    for field in tostring(content or ""):gmatch("%S+") do fields[#fields + 1] = field end
    local expires = tonumber(fields[1])
    local scope = fields[2] or "match"
    if expires == nil or (scope ~= "match" and scope ~= "dialogue") then
        return false
    end
    if #fields ~= 1 and #fields ~= 2 then
        if #fields ~= 4 or not fields[3]:match("^[%w%-]+$")
            or (fields[4] ~= "full" and fields[4] ~= "cursor") then return false end
    end
    if expires < os.time() then
        -- Automatic lease expiry is a pause, distinct from an explicit zero
        -- release, a malformed/deleted marker or a dialogue lifecycle change.
        return false, nil, nil, expires > 0 and scope == "match"
    end
    if #fields == 1 or #fields == 2 then return scope end
    return scope, fields[3], fields[4]
end

local function probe_requested()
    local scope = demand.read_probe_request()
    return scope
end

local function idle_snapshot(keep_caches)
    -- An expired request is a lifecycle boundary. Clearing primitive/path
    -- state needs no UObject calls, including when a travel unload is active.
    if not keep_caches then
        cached_ui_object, cached_ui_class, cached_manager = nil, nil, nil
        cached_hand_roots, cached_full_widgets, cached_hand_actors = {}, nil, nil
        card_widget_reference_cache, board_face_cache = {}, {}
        last_ui_lookup_at, last_manager_lookup_at, last_manager_fallback_at = 0, 0, 0
        cached_hand_roots_at, last_card_image_identity_debug_at = 0, 0
        live_card_identity_by_slot, last_card_indices = {}, {}
        live_card_focus_slot, live_card_focus_source, last_manager_identity = nil, nil, nil
        manager_hand_seen = false
        hand_ids.seen, hand_ids.read, hand_ids.read_at, hand_ids.missing = nil, nil, nil, true
        live_card_probe.slot_diagnostics = {}
        board_flag_cache, board_card_owner, board_cursor_cache, board_owner_manager = {}, {}, {}, nil
        demand.match_root = {path = nil, identity = nil, lookup_at = nil}
        demand.board_owner_identity, demand.suspended_board = nil, nil
        deck_screen_cache = {root = nil, top = nil, scanned_at = nil}
        talk_menu_cache = {menu = nil, scanned_at = nil}
        deck_detail_cache = {at = nil, value = nil, discovered_at = nil, blocks = nil}
        deck_billboard_cache = {at = nil, items = {}}
        demand.census = {key = nil, cells = {}, boxes = {}}
        deck_screen_events = {}
        deck_editor.owner, deck_editor.owner_top = nil, nil
        deck_editor.grids, deck_editor.units, deck_editor.rows = {}, {}, {}
        deck_editor.cursors, deck_editor.filters, deck_editor.strip_presses = {}, {}, nil
    end
    return {
        schema_version = 1,
        active = false,
        phase = "inactive",
        turn = 0,
        begin_match_available = false,
        match_screen = false,
        probe_idle = true,
        talk_menu = {observed = false},
        cursor = {area = "unknown"},
        hand = {},
        legal_moves = {},
        board = {rows = 3, columns = 5, cells = {}},
        reflection = {
            manager_found = false,
            hand_observed = false,
            legal_moves_observed = false,
            board_observed = false,
        },
    }
end

local last_probe_scope = nil

local function scoped_snapshot(scope, sequence, mode)
    if scope ~= last_probe_scope then
        idle_snapshot() -- clear stale paths once at each lifecycle boundary
        last_probe_scope = scope
    end
    if scope == "match" then
        local state, reason
        if mode == "cursor" then
            state = demand.encode_deck_select_snapshot(true)
            if not state.deck_screen.observed then
                -- A worker may first read this long-lived deck screen after
                -- the cached root was evicted. Recover the bounded deck root
                -- without running manager, hand or board discovery.
                state = demand.encode_deck_select_snapshot(false)
            end
            if not state.deck_screen.observed then
                state = idle_snapshot(true)
            end
        else
            state, reason = encode_snapshot(sequence)
        end
        if state then state.probe_scope = "match" end
        return state, reason
    end
    -- Retain the bounded Talk lookup between dialogue samples instead of
    -- restarting its five-second discovery limit on every sample.
    local state = idle_snapshot(true)
    state.probe_scope = scope or "idle"
    if scope == "dialogue" then
        state.talk_menu = timed("encode_talk_menu", encode_talk_menu)
    end
    return state
end

demand.last_ticket = nil

-- One explicit ticket, one fresh sample. The existing timer only services
-- requests; it never rescans a stationary screen after acknowledging a ticket.
function demand.service_probe_request(scope, ticket, mode, sequence, expired_match)
    if (not scope and not expired_match) or (scope and scope ~= "match") then
        demand.suspended_board = nil
    end
    if scope ~= last_probe_scope then
        local suspended = demand.suspended_board
        if not scope and expired_match and last_probe_scope == "match" then
            suspended = {manager = demand.board_owner_identity, cards = board_card_owner}
        end
        idle_snapshot()
        if expired_match or scope == "match" then demand.suspended_board = suspended end
        last_probe_scope = scope
        demand.last_ticket = nil
        if not scope then
            local state = idle_snapshot(true)
            state.probe_scope = "idle"
            return state
        end
    end
    if not scope or not ticket or ticket == "-" or ticket == demand.last_ticket then
        return nil, "awaiting_request"
    end
    local state, reason = scoped_snapshot(scope, sequence, mode)
    if state then state.probe_request = ticket; state.probe_read = mode end
    return state, reason
end

local function sample()
    local scope, ticket, mode, expired_match = demand.read_probe_request()
    local sequence = read_sequence()
    if not sequence then return false, "state_sequence_unavailable" end
    local state, reason = demand.service_probe_request(scope, ticket, mode, sequence, expired_match)
    if not state then return reason == "awaiting_request", reason end
    local payload = {
        schema_version = 1,
        base_sequence = sequence,
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        source = "ue4ss_queen_blood_read_only_probe",
        queen_blood = state,
    }
    local ok = timed("write_snapshot", write_snapshot, payload)
    if ok then last_sample_sequence = sequence; demand.last_ticket = ticket end
    return ok, nil
end

local last_status_key = nil

local function record_status(status, detail)
    local key = status == "published" and status or (status .. "|" .. detail)
    if key == last_status_key then return end
    last_status_key = key
    pcall(function()
        local file = io.open(STATUS_PATH, "wb")
        if not file then return end
        file:write(os.date("!%Y-%m-%dT%H:%M:%SZ") .. "|" .. status .. "|" .. detail:sub(1, 512) .. "\n")
        file:close()
    end)
end

-- Cumulative game-thread cost of this probe, for comparing runs.
local TIMING_PATH = ROOT .. "queen_blood_probe.timing.json"
local timing_clock = (os and os.clock) or function() return 0 end
local timing = {calls = 0, total_ms = 0, max_ms = 0, over_frame = 0}

local function record_timing(started)
    local elapsed = (timing_clock() - started) * 1000
    timing.calls = timing.calls + 1
    timing.total_ms = timing.total_ms + elapsed
    if elapsed > timing.max_ms then timing.max_ms = elapsed end
    if elapsed > 16.7 then timing.over_frame = timing.over_frame + 1 end
    if timing.calls % 10 ~= 0 then return end
    pcall(function()
        local file = io.open(TIMING_PATH, "wb")
        if not file then return end
        local sections = {}
        for name, entry in pairs(section_timing) do
            sections[#sections + 1] = string.format('"%s":{"calls":%d,"total_ms":%.1f,"max_ms":%.1f}',
                name, entry.calls, entry.total_ms, entry.max_ms)
        end
        table.sort(sections)
        file:write(string.format('{"calls":%d,"total_ms":%.1f,"max_ms":%.1f,"over_frame_calls":%d,"sections":{%s}}\n',
            timing.calls, timing.total_ms, timing.max_ms, timing.over_frame, table.concat(sections, ",")))
        file:close()
    end)
end

-- Loaded once; explicit expiring requests are the only way to invoke it.
deck_editor.enumerator = safe(nil, function() return require("card_enumerator") end)

deck_editor.find_owner = function()
    local top = deck_screen_top()
    local top_path = census_path(top)
    if not top_path then return nil end
    local function belongs(owner)
        local path = valid(owner) and census_path(owner) or nil
        return path and path:sub(1, #top_path + 1) == top_path .. "."
            and path:match("%.EndNewCardDeckWindow_%d+$") ~= nil
    end
    local cached = refs.resolve(deck_editor.owner)
    if deck_editor.owner_top == top_path and belongs(cached) then return cached end
    deck_editor.owner, deck_editor.owner_top = nil, nil
    -- List population is native and need not emit a selection
    -- callback. Find this root's controller before any cursor input.
    local found = safe(nil, function() return FindAllOf("EndNewCardDeckWindow") end)
    if type(found) ~= "table" or #found > 64 then return nil end
    local owner
    for _, candidate in ipairs(found) do
        if belongs(candidate) then
            if owner then return nil end -- ambiguity is never guessed
            owner = candidate
        end
    end
    if owner then deck_editor.owner, deck_editor.owner_top = refs.path(owner), top_path end
    return owner
end

function FF7RQueenBloodReadOnlyTick()
    local started = timing_clock()
    if deck_editor.enumerator then
        pcall(deck_editor.enumerator.tick, ROOT, {
            valid = valid, top = deck_screen_top, shown = widget_shown,
            owner = deck_editor.find_owner,
            member = member, path = census_path, card = deck_card_cell,
            address = function(object)
                return safe(nil, function() return string.format("0x%x", object:GetAddress()) end)
            end,
            find = function(path) return safe(nil, function() return StaticFindObject(path) end) end,
            busy = function(value) deck_editor.enumerating = value end,
            json = json,
            guard = function(top)
                return {cursors = encode_deck_editor().cursors,
                    total = member_text(member(top, "CardList_Player"), "Txt_TotalNumber"),
                    shown = widget_shown(top),
                    edit_shown = widget_shown(member(top, "Card_EditListNew")),
                    strip_total = member_text(member(top, "CardDeckList"), "Txt_TotalNumber"),
                    guide = member_text(top, "Txt_Guide"), filters = (function()
                        local events, path = {}, census_path(top)
                        for _, event in ipairs(deck_editor.filters) do
                            if not event.root or event.root == path then events[#events + 1] = event end
                        end
                        return events
                    end)()}
            end,
        })
    end
    local call_ok, sample_ok, reason = pcall(sample)
    local status = call_ok and (sample_ok and "published" or "rejected") or "error"
    local detail = tostring(reason or sample_ok or "")
    write_debug(GAME_DEBUG_PATH, status, detail)
    record_status(status, detail)
    record_timing(started)
end

local function run_probe_on_game_thread()
    pcall(FF7RQueenBloodReadOnlyTick)
end

pcall(register_card_focus_hooks)
-- The deck editor fills its grids when the deck screen is built, before the
-- probe first sees that screen, so its cell callbacks are hooked at load.
pcall(register_editor_hooks)

-- The identity path is read directly from the live EndBillboard objects.  The
-- earlier title/image setter hooks remain in this source as bounded diagnostic
-- helpers, but are deliberately not registered in production: runtime
-- validation showed no useful assignment events from them.

-- ProcessEvent-based ExecuteInGameThread stops dispatching while the card UI
-- owns the transition. Prefer UE4SS's native game-thread timers, using the
-- EngineTick variant when available and the delayed game-thread loop as the
-- compatible fallback.
if type(LoopInGameThreadAfterFrames) == "function" and EngineTickAvailable then
    LoopInGameThreadAfterFrames(30, run_probe_on_game_thread)
elseif type(LoopInGameThreadWithDelay) == "function" then
    LoopInGameThreadWithDelay(SAMPLE_PERIOD_MS, run_probe_on_game_thread)
else
    LoopAsync(SAMPLE_PERIOD_MS, function()
        write_debug(WORKER_DEBUG_PATH, "scheduled", read_sequence())
        pcall(ExecuteInGameThread, run_probe_on_game_thread)
        return false
    end)
end
