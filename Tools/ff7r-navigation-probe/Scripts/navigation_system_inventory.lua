-- Read-only navigation-system inventory.
--
-- This is deliberately separate from navmesh_path.lua.  It does not call
-- FindAllOf, inspect AI actors, issue movement requests, or invoke native
-- pathfinding.  Its only purpose is to establish whether the live world has
-- a navigation system and a MainNavData object that can be handed to a
-- future, separately validated native bridge.

local OUTPUT_PATH = "C:\\CaitGPT\\Telemetry\\navigation-system-inventory.json"
local TEMP_PATH = OUTPUT_PATH .. ".tmp"
local SAMPLE_PATH = "C:\\CaitGPT\\Telemetry\\state.sample"
local SAMPLE_PERIOD_MS = 2000
local MAX_ATTEMPTS = 120
local SCHEMA_VERSION = 1
local UEHelpers = require("UEHelpers")

local attempts = 0

local function safe(default, callback)
    local ok, value = pcall(callback)
    if ok and value ~= nil then return value end
    return default
end

local function valid(object)
    return safe(false, function() return object ~= nil and object:IsValid() end)
end

local function text(value)
    if value == nil then return nil end
    local converted = safe(nil, function() return value:ToString() end)
    return tostring(converted ~= nil and converted or value)
end

local function finite(value)
    return type(value) == "number" and value == value and value > -1.0e30 and value < 1.0e30
end

local function json_escape(value)
    local escaped = tostring(value or "")
    escaped = escaped:gsub("\\", "\\\\"):gsub('"', '\\"')
    return '"' .. escaped .. '"'
end

local function json_encode(value)
    local value_type = type(value)
    if value == nil then return "null" end
    if value_type == "string" then return json_escape(value) end
    if value_type == "boolean" then return value and "true" or "false" end
    if value_type == "number" then return finite(value) and tostring(value) or "null" end
    if value_type ~= "table" then return "null" end
    local is_array = true
    local maximum = 0
    for key, _ in pairs(value) do
        if type(key) ~= "number" or key < 1 or math.floor(key) ~= key then
            is_array = false
            break
        end
        if key > maximum then maximum = key end
    end
    local fields = {}
    if is_array then
        for index = 1, maximum do fields[#fields + 1] = json_encode(value[index]) end
        return "[" .. table.concat(fields, ",") .. "]"
    end
    for key, item in pairs(value) do
        fields[#fields + 1] = json_escape(key) .. ":" .. json_encode(item)
    end
    return "{" .. table.concat(fields, ",") .. "}"
end

local function object_name(object)
    if not valid(object) then return nil end
    return safe(nil, function() return object:GetFullName() end)
end

local function object_class(object)
    if not valid(object) then return nil end
    local class = safe(nil, function() return object:GetClass() end)
    return object_name(class)
end

local function read_field(object, field_name)
    if not valid(object) then return nil end
    return safe(nil, function() return object[field_name] end)
end

local function scalar_field(object, field_name)
    local value = read_field(object, field_name)
    if value == nil then return nil end
    if type(value) == "number" or type(value) == "boolean" or type(value) == "string" then
        return value
    end
    return text(value)
end

local function config_record(nav_data)
    local config = read_field(nav_data, "NavDataConfig")
    if config == nil then return nil end
    return {
        name = scalar_field(config, "Name"),
        agent_radius = scalar_field(config, "AgentRadius"),
        agent_height = scalar_field(config, "AgentHeight"),
        agent_step_height = scalar_field(config, "AgentStepHeight"),
        nav_data_class = text(read_field(config, "NavDataClass")),
    }
end

local function read_sequence()
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then return nil end
    local line = file:read("*l")
    file:close()
    local sequence = line and line:match("^(%d+)|")
    return sequence and tonumber(sequence) or nil
end

local function write(record)
    local file = io.open(TEMP_PATH, "wb")
    if not file then return end
    file:write(json_encode(record) .. "\n")
    file:flush()
    file:close()
    os.remove(OUTPUT_PATH)
    os.rename(TEMP_PATH, OUTPUT_PATH)
end

local function sample()
    attempts = attempts + 1
    local sequence = read_sequence() or 0
    local player_controller = safe(nil, function() return UEHelpers.GetPlayerController() end)
    local pawn = valid(player_controller) and safe(nil, function() return player_controller.Pawn end) or nil
    local world = valid(pawn) and safe(nil, function() return pawn:GetWorld() end) or nil
    local navigation_system = valid(world) and read_field(world, "NavigationSystem") or nil
    local main_nav_data = valid(navigation_system) and read_field(navigation_system, "MainNavData") or nil
    local abstract_nav_data = valid(navigation_system) and read_field(navigation_system, "AbstractNavData") or nil

    local record = {
        schema_version = SCHEMA_VERSION,
        source = "ue4ss_navigation_system_inventory",
        movement_sent = false,
        ai_actor_enumeration = false,
        native_path_query_called = false,
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        attempts = attempts,
        base_sequence = sequence,
        world = {
            valid = valid(world),
            full_name = object_name(world),
            class = object_class(world),
        },
        navigation_system = {
            valid = valid(navigation_system),
            full_name = object_name(navigation_system),
            class = object_class(navigation_system),
            default_agent_name = scalar_field(navigation_system, "DefaultAgentName"),
            main_nav_data = {
                valid = valid(main_nav_data),
                full_name = object_name(main_nav_data),
                class = object_class(main_nav_data),
                config = config_record(main_nav_data),
                runtime_generation = scalar_field(main_nav_data, "RuntimeGeneration"),
                data_version = scalar_field(main_nav_data, "DataVersion"),
            },
            abstract_nav_data = {
                valid = valid(abstract_nav_data),
                full_name = object_name(abstract_nav_data),
                class = object_class(abstract_nav_data),
            },
        },
    }
    if not valid(player_controller) or not valid(pawn) or not valid(world) then
        record.status = "waiting_for_gameplay"
        record.reason_code = "world_or_pawn_unavailable"
    elseif not valid(navigation_system) then
        record.status = "navigation_unavailable"
        record.reason_code = "world_navigation_system_missing"
    elseif not valid(main_nav_data) then
        record.status = "navdata_unavailable"
        record.reason_code = "main_nav_data_missing"
    else
        record.status = "main_nav_data_available"
        record.reason_code = "live_navigation_system_and_main_nav_data"
    end
    write(record)
end

write({
    schema_version = SCHEMA_VERSION,
    source = "ue4ss_navigation_system_inventory",
    status = "waiting_for_gameplay",
    movement_sent = false,
    ai_actor_enumeration = false,
    native_path_query_called = false,
})

LoopAsync(SAMPLE_PERIOD_MS, function()
    if attempts >= MAX_ATTEMPTS then return true end
    ExecuteInGameThread(function()
        pcall(sample)
    end)
    return false
end)
