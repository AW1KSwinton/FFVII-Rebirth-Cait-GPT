local SAMPLE_PATH = "C:\\CaitGPT\\Telemetry\\state.sample"
local SIDECAR_PATH = "C:\\CaitGPT\\Telemetry\\surroundings.json"
local UEHelpers = require("UEHelpers")

-- This is an exploratory index, not a control loop.  Keep the scan light on
-- lower-end machines and let the base exporter provide the high-rate state.
local SAMPLE_PERIOD_MS = 2000
local MAX_FIELD_ACTION_ACTORS = 96
local MAX_CHARACTER_ACTORS = 96
local NEARBY_RADIUS_CM = 2500.0

-- Keep this list narrow until each class is runtime-validated.  The base class
-- may already include derived instances in FindAllOf; identity de-duplication
-- below makes that harmless.
local FIELD_ACTION_CLASSES = {
    "EndFieldActionActorBase",
    "EndFieldActionActorBenchBreak",
    "EndFieldActionActorVendingMachine",
    "EndFieldActionActorSimpleInteract",
}

local function safe_call(default, callback)
    local ok, value = pcall(callback)
    if not ok or value == nil then
        return default
    end
    return value
end

local function object_valid(value)
    return safe_call(false, function()
        return value ~= nil and value:IsValid()
    end)
end

local function finite_number(value)
    return type(value) == "number" and value == value and
        value > -1.0e30 and value < 1.0e30
end

local function stringify(value)
    if value == nil then
        return ""
    end
    local converted = safe_call(nil, function() return value:ToString() end)
    return tostring(converted or value)
end

local function json_escape(value)
    local text = stringify(value)
    text = text:gsub("\\", "\\\\")
    text = text:gsub('"', '\\"')
    text = text:gsub("\r", "\\r")
    text = text:gsub("\n", "\\n")
    return '"' .. text .. '"'
end

local function json_number(value)
    if not finite_number(value) then
        return "null"
    end
    return string.format("%.6f", value)
end

local function json_position(location)
    if not location or not finite_number(location.X) or
       not finite_number(location.Y) or not finite_number(location.Z) then
        return nil
    end
    return string.format(
        '{"x":%s,"y":%s,"z":%s}',
        json_number(location.X), json_number(location.Y), json_number(location.Z)
    )
end

local function read_base_sequence()
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then
        return nil
    end
    local line = file:read("*l")
    file:close()
    if not line then
        return nil
    end
    local sequence = string.match(line, "^(%d+)|")
    return sequence and tonumber(sequence) or nil
end

local function actor_identity(actor)
    return safe_call("", function() return actor:GetFullName() end)
end

local function actor_label(actor)
    -- Generic EndCharacter/field-action results can contain stale UObject
    -- proxies.  GetHumanReadableName() logs a native nullptr error for those
    -- proxies even when wrapped in pcall, which aborts payload construction.
    -- The stable full object name is sufficient for the first-pass index and
    -- avoids invoking a fragile display-name helper in the runtime loop.
    local identity = actor_identity(actor)
    if identity ~= "" then
        return identity
    end
    return "actor"
end

local function actor_location(actor)
    return safe_call(nil, function() return actor:K2_GetActorLocation() end)
end

local function actor_distance(player, actor)
    local distance = safe_call(-1, function() return player:GetDistanceTo(actor) end)
    if not finite_number(distance) or distance < 0 then
        return nil
    end
    return distance
end

local function encode_field_action(actor, player, seen)
    if not object_valid(actor) then
        return nil
    end
    local identity = actor_identity(actor)
    if identity == "" or seen[identity] then
        return nil
    end
    seen[identity] = true
    local distance = actor_distance(player, actor)
    local position = json_position(actor_location(actor))
    if not distance or distance > NEARBY_RADIUS_CM or not position then
        return nil
    end
    local press_volume = safe_call(nil, function() return actor.PressVolume end)
    local action_type = safe_call(nil, function() return press_volume.ActionType end)
    local fields = {
        '"id":' .. json_escape(identity),
        '"kind":"field_action"',
        '"label":' .. json_escape(actor_label(actor)),
        '"distance":' .. json_number(distance / 100.0),
        '"usable":false',
        '"position":' .. position,
    }
    if type(action_type) == "number" and action_type >= 0 then
        fields[#fields + 1] = '"action_type":' .. tostring(action_type)
    end
    return "{" .. table.concat(fields, ",") .. "}"
end

local function encode_character(actor, player, seen)
    if not object_valid(actor) or actor == player then
        return nil
    end
    local identity = actor_identity(actor)
    if identity == "" or seen[identity] then
        return nil
    end
    seen[identity] = true
    local distance = actor_distance(player, actor)
    local position = json_position(actor_location(actor))
    if not distance or distance > NEARBY_RADIUS_CM or not position then
        return nil
    end
    return "{" .. table.concat({
        '"id":' .. json_escape(identity),
        '"kind":"character"',
        '"name":' .. json_escape(actor_label(actor)),
        '"distance":' .. json_number(distance / 100.0),
        '"position":' .. position,
    }, ",") .. "}"
end

local function collect_field_actions(player)
    local values = {}
    local seen = {}
    for _, class_name in ipairs(FIELD_ACTION_CLASSES) do
        local actors = safe_call(nil, function() return FindAllOf(class_name) end)
        if type(actors) == "table" then
            for _, actor in ipairs(actors) do
                if #values >= MAX_FIELD_ACTION_ACTORS then
                    return values
                end
                local encoded = encode_field_action(actor, player, seen)
                if encoded then
                    values[#values + 1] = encoded
                end
            end
        end
    end
    return values
end

local function collect_characters(player)
    local values = {}
    local seen = {}
    local actors = safe_call(nil, function() return FindAllOf("EndCharacter") end)
    if type(actors) ~= "table" then
        return values
    end
    for _, actor in ipairs(actors) do
        if #values >= MAX_CHARACTER_ACTORS then
            break
        end
        local encoded = encode_character(actor, player, seen)
        if encoded then
            values[#values + 1] = encoded
        end
    end
    return values
end

local function write_sidecar_on_game_thread()
    local sequence = read_base_sequence()
    local controller = safe_call(nil, function() return UEHelpers.GetPlayerController() end)
    local player = object_valid(controller) and safe_call(nil, function() return controller.Pawn end) or nil
    if not sequence then
        return
    end
    local player_available = object_valid(player)
    local field_actions = player_available and collect_field_actions(player) or {}
    local characters = player_available and collect_characters(player) or {}
    local payload = "{" .. table.concat({
        '"schema_version":1',
        '"base_sequence":' .. tostring(sequence),
        '"timestamp_utc":' .. json_escape(os.date("!%Y-%m-%dT%H:%M:%SZ")),
        '"player_available":' .. (player_available and "true" or "false"),
        '"nearby_interactables":[' .. table.concat(field_actions, ",") .. "]",
        '"nearby_actors":[' .. table.concat(characters, ",") .. "]",
    }, ",") .. "}\n"
    local file = io.open(SIDECAR_PATH, "wb")
    if file then
        file:write(payload)
        file:flush()
        file:close()
    end
end

LoopAsync(SAMPLE_PERIOD_MS, function()
    ExecuteInGameThread(function()
        pcall(write_sidecar_on_game_thread)
    end)
    return false
end)
