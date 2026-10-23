-- Read-only live map UI exporter.
--
-- This probe runs only while the base telemetry says the game is paused.  It
-- performs bounded reads from an allow-list of map/widget classes and writes
-- a sequence-bound sidecar.  It never sends input or changes game state.

local SAMPLE_PATH = "C:\\CaitGPT\\Telemetry\\state.sample"
local STATE_PATH = "C:\\CaitGPT\\Telemetry\\state.json"
local OUTPUT_PATH = "C:\\CaitGPT\\Telemetry\\map-surface-probe.txt"
local MAP_UI_PATH = "C:\\CaitGPT\\Telemetry\\map-ui.json"
local MAP_UI_TMP_PATH = "C:\\CaitGPT\\Telemetry\\map-ui.json.tmp"
local SAMPLE_PERIOD_MS = 2000
local MAX_MARKERS = 256
local MAX_BROAD_WIDGETS = 192
local MAX_WIDGETS = MAX_BROAD_WIDGETS
local MAX_EXACT_INSTANCES = 64

local stable_samples = 0
local previous_fingerprint = ""
local complete = false
local last_context_sequence = nil
local context_progress = 0
local pending_sequence = nil
local pending_map_id = nil

local PAGE_CLASSES = {
    "Map_Base_C",
    "MapMenu_Base_C",
    "WorldMap_C",
    "MapWidget_C",
    "Pause_00_C",
}
local MARKER_CLASSES = {
    "MapMarker_C",
    "MapIcon_C",
    "MapPoint_C",
    "MapPOI_C",
}
local TOKENS = {"map", "marker", "icon", "poi", "pin"}

local function safe(default, callback)
    local ok, value = pcall(callback)
    if ok and value ~= nil then return value end
    return default
end

local function valid(object)
    return safe(false, function()
        return object ~= nil and object:IsValid()
    end)
end

local function text(value)
    if value == nil then return "" end
    local value_type = type(value)
    if value_type == "string" then return value end
    if value_type == "number" or value_type == "boolean" then
        return tostring(value)
    end
    -- Unreal wrappers can be stale or null between the bounded object scan
    -- and property conversion. Use only a protected conversion; never call
    -- the UObject ToString method directly on an untrusted wrapper.
    local ok, result = pcall(function() return tostring(value) end)
    return ok and result or ""
end

local function finite(value)
    return type(value) == "number" and value == value and
        value > -1.0e30 and value < 1.0e30
end

local function json_escape(value)
    local result = text(value)
    result = result:gsub("\\", "\\\\"):gsub('"', '\\"')
    result = result:gsub("\r", "\\r"):gsub("\n", "\\n")
    return '"' .. result .. '"'
end

local function json_number(value)
    if not finite(value) then return "null" end
    return string.format("%.6f", value)
end

local function json_bool(value)
    return value and "true" or "false"
end

local function json_position(value)
    if value == nil then return nil end
    local x = safe(nil, function() return value.X end)
    local y = safe(nil, function() return value.Y end)
    local z = safe(nil, function() return value.Z end)
    if not finite(x) or not finite(y) or not finite(z) then
        return nil
    end
    return string.format('{"x":%s,"y":%s,"z":%s}',
        json_number(x), json_number(y), json_number(z))
end

local function identity(object)
    return safe("", function() return object:GetFullName() end)
end

local function has_token(value, tokens)
    local lowered = tostring(value or ""):lower()
    for _, token in ipairs(tokens) do
        if lowered:find(token, 1, true) then return true end
    end
    return false
end

local function read_property(object, names)
    for _, name in ipairs(names) do
        local value = safe(nil, function() return object[name] end)
        if value ~= nil then return value, name end
    end
    return nil, nil
end

local function read_string_property(object, names)
    local value = read_property(object, names)
    if value == nil then return "" end
    return text(value)
end

local function visible(object)
    return safe(false, function() return object:GetIsVisible() end) or
        safe(false, function() return object:IsInViewport() end)
end

local function read_context()
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then return nil, nil end
    local sample = file:read("*a") or ""
    file:close()
    local sequence = string.match(sample, "^(%d+)|")
    if not sequence then
        sequence = string.match(sample, '"sequence"%s*:%s*(%d+)')
    end

    local state_file = io.open(STATE_PATH, "rb")
    local state = state_file and (state_file:read("*a") or "") or ""
    if state_file then state_file:close() end
    local map_id = string.match(state, '"map_id"%s*:%s*"([^"]*)"')
    local paused = string.match(state, '"paused"%s*:%s*true') ~= nil
    local running = string.match(state, '"running"%s*:%s*true') ~= nil
    local live = string.match(state, '"live"%s*:%s*true') ~= nil
    return sequence and tonumber(sequence) or nil, map_id, paused, running, live
end
local function describe_page(object)
    if not valid(object) then return nil end
    local page_visible = visible(object)
    local name = identity(object)
    if name == "" or not has_token(name, {"map", "worldmap"}) then return nil end
    local selected = read_string_property(object, {
        "SelectedMarkerId", "SelectedPOIId", "SelectedId", "CurrentSelectionId"
    })
    local tab = read_string_property(object, {"CurrentPage", "PageName", "ActiveTab"})
    local layer = read_string_property(object, {"MapLayer", "LayerName", "WorldLayer"})
    local zoom = read_property(object, {"Zoom", "ZoomLevel", "MapZoom"})
    local fields = {
        '"open":' .. json_bool(page_visible),
        '"id":' .. json_escape(name),
        '"page":' .. json_escape(tab),
        '"selected_id":' .. json_escape(selected),
        '"layer":' .. json_escape(layer),
    }
    if type(zoom) == "number" and finite(zoom) then
        fields[#fields + 1] = '"zoom":' .. json_number(zoom)
    end
    return "{" .. table.concat(fields, ",") .. "}"
end

local function describe_marker(object, seen)
    if not valid(object) then return nil end
    local marker_visible = visible(object)
    local name = identity(object)
    if name == "" or not has_token(name, {"marker", "icon", "poi", "pin"}) then return nil end
    if seen[name] then return nil end
    seen[name] = true

    local marker_id = read_string_property(object, {
        "MarkerId", "MapMarkerId", "MapIconId", "IconId", "POIId", "Id"
    })
    if marker_id == "" then marker_id = name end
    local icon_type = read_string_property(object, {
        "IconType", "MapIconType", "IconTypeName", "IconCategory"
    })
    local subicon = read_string_property(object, {
        "SubIcon", "SubIconType", "IconState", "State", "Badge"
    })
    local label = read_string_property(object, {
        "Label", "DisplayName", "Name", "Text"
    })
    local target_id = read_string_property(object, {
        "TargetObjectId", "TargetActorId", "ObjectId", "ActorId"
    })
    local map_position = read_property(object, {
        "MapPosition", "CanvasPosition", "ScreenPosition", "Position"
    })
    local world_position = read_property(object, {
        "WorldPosition", "TargetPosition", "WorldLocation"
    })
    local map_json = json_position(map_position)
    local world_json = json_position(world_position)
    local fields = {
        '"marker_id":' .. json_escape(marker_id),
        '"runtime_id":' .. json_escape(name),
        '"visible":' .. json_bool(marker_visible),
        '"icon_type":' .. json_escape(icon_type),
        '"subicon":' .. json_escape(subicon),
        '"label":' .. json_escape(label),
        '"target_id":' .. json_escape(target_id),
    }
    if map_json then fields[#fields + 1] = '"map_position":' .. map_json end
    if world_json then fields[#fields + 1] = '"world_position":' .. world_json end
    return "{" .. table.concat(fields, ",") .. "}"
end

local function collect_widgets()
    local pages = {}
    local markers = {}
    local seen_markers = {}
    local seen_pages = {}

    for _, class_name in ipairs(PAGE_CLASSES) do
        local objects = safe(nil, function() return FindAllOf(class_name) end)
        if type(objects) == "table" then
            local inspected = 0
            for _, object in ipairs(objects) do
                if inspected >= MAX_EXACT_INSTANCES then break end
                inspected = inspected + 1
                local page = describe_page(object)
                if page and not seen_pages[page] then
                    seen_pages[page] = true
                    pages[#pages + 1] = page
                    break
                end
            end
        end
    end

    for _, class_name in ipairs(MARKER_CLASSES) do
        local objects = safe(nil, function() return FindAllOf(class_name) end)
        if type(objects) == "table" then
            local inspected = 0
            for _, object in ipairs(objects) do
                if inspected >= MAX_EXACT_INSTANCES then break end
                inspected = inspected + 1
                if #markers >= MAX_MARKERS then break end
                local marker = describe_marker(object, seen_markers)
                if marker then markers[#markers + 1] = marker end
            end
        end
    end

    -- Do not enumerate the global UserWidget set here. The map screen owns
    -- many transient widgets and that broad UObject traversal is unsafe in
    -- the live game thread. Exact allow-listed classes above are bounded.
    table.sort(markers)
    return pages[1], markers
end

local function write_text_evidence(sequence, page, marker_count)
    local file = io.open(OUTPUT_PATH, "w")
    if not file then return end
    file:write("status=complete\n")
    file:write("timestamp_utc=" .. os.date("!%Y-%m-%dT%H:%M:%SZ") .. "\n")
    file:write("base_sequence=" .. tostring(sequence or "") .. "\n")
    file:write("page_resolved=" .. tostring(page ~= nil) .. "\n")
    file:write("markers=" .. tostring(marker_count or 0) .. "\n")
    file:write("scan_policy=paused_only_exact_classes\n")
    file:flush()
    file:close()
end

local function write_sidecar(sequence, map_id, page, markers, status, reason)
    local marker_json = table.concat(markers or {}, ",")
    local fingerprint = tostring(page or "") .. "|" .. marker_json
    if fingerprint == previous_fingerprint then
        stable_samples = stable_samples + 1
    else
        stable_samples = 1
        previous_fingerprint = fingerprint
    end
    local payload = "{" .. table.concat({
        '"schema_version":1',
        '"base_sequence":' .. tostring(sequence or 0),
        '"timestamp_utc":' .. json_escape(os.date("!%Y-%m-%dT%H:%M:%SZ")),
        '"status":' .. json_escape(status),
        '"map_id":' .. json_escape(map_id or ""),
        '"stability":{"samples":' .. tostring(stable_samples) ..
            ',"same_page":' .. json_bool(stable_samples >= 2) ..
            ',"same_markers":' .. json_bool(stable_samples >= 2) .. "}",
        '"map_state":{"page":' .. (page or '{"open":false}') ..
            ',"markers":[' .. marker_json .. "]}"
    }, ",") .. "}\n"
    local file = io.open(MAP_UI_TMP_PATH, "wb")
    if not file then return end
    file:write(payload)
    file:flush()
    file:close()
    os.remove(MAP_UI_PATH)
    os.rename(MAP_UI_TMP_PATH, MAP_UI_PATH)
end

local function sample_on_game_thread()
    -- ExecuteInGameThread must not perform telemetry file I/O in this build.
    -- The async loop validates the context and publishes these two values
    -- before scheduling this bounded UE4SS object read.
    local sequence = pending_sequence
    local map_id = pending_map_id
    if not sequence or not map_id or map_id == "" then return end

    local ok, page, markers = pcall(collect_widgets)
    if not ok then
        write_text_evidence(sequence, nil, 0)
        write_sidecar(sequence, map_id, nil, {}, "unavailable", "collect_widgets_failed")
        return
    end
    write_text_evidence(sequence, page, #markers)
    if not page then
        write_sidecar(sequence, map_id, nil, markers, "unavailable", "map_page_not_resolved")
        return
    end
    local status = "unavailable"
    if stable_samples >= 2 then status = "ready" end
    write_sidecar(sequence, map_id, page, markers, status, nil)
    if stable_samples >= 2 then complete = true end
end

write_sidecar(0, "", nil, {}, "waiting", "probe_start")

LoopAsync(SAMPLE_PERIOD_MS, function()
    if complete then return true end

    -- Read the base exporter state on the LoopAsync side.  Only after a live,
    -- advancing, paused map context is proven do we enter the game thread.
    local sequence, map_id, paused, running, live = read_context()
    if not live or not running or not paused or not sequence or not map_id or map_id == "" then
        last_context_sequence = sequence
        context_progress = 0
        return false
    end
    if last_context_sequence == nil or sequence <= last_context_sequence then
        last_context_sequence = sequence
        context_progress = 0
        return false
    end
    last_context_sequence = sequence
    context_progress = context_progress + 1
    if context_progress < 2 then return false end

    pending_sequence = sequence
    pending_map_id = map_id
    ExecuteInGameThread(sample_on_game_thread)
    return false
end)
