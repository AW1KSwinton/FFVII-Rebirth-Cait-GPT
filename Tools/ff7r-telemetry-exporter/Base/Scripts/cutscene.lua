-- Cutscene detection (read-only; no input, no state change).
--
-- Cutscenes play through level sequence players; the game's event scenes use
-- EndCinemaSequencePlayer. Live 2026-10-05 at Gongaga's Chadley, the combat
-- simulator's intro was EV_COMON_2112, one of three cinema players loaded
-- long before it played. So players are found by an occasional object scan
-- (FindAllOf("LevelSequencePlayer") includes the game's subclasses) and each
-- tick only asks the known players whether they play:
--   - when an event takes Cloud (his pawn is released: conversations,
--     events, loads), then every second for the next five seconds;
--   - every 30 s otherwise, as the exporter rescans its menu widget.
-- Cloud's own signals (bPlayingCutScene, the camera's view target) go blank
-- when an event takes him, so they are reported but do not decide.
--
-- What it must not do, each learned live on 2026-10-05:
-- - scan every tick (eight scans per read froze the game);
-- - name a member the object does not have, even inside pcall: each failed
--   lookup (camera:GetViewTarget(), which is not a UFunction) leaked Lua's
--   C-call budget, and within seconds every module of the exporter failed
--   with "C stack overflow" and the map vanished from telemetry. A failed
--   class.member read is remembered and never tried again;
-- - register NotifyOnNewObject (it broke the Lua state during a load);
-- - call the game's field API every tick from the title screen (the game
--   stopped at startup with "Fatal error!" on every launch).
--
-- State file (cutscene.state, JSON), written when it changes and at least
-- once a second:
--   {"schema_version":4,"sequence":N,"timestamp_utc":"...","leader":true|false,
--    "tracked":K,"anim_cutscene":..,"view_target":"...",
--    "playing":[{"kind":"cinema","player_class":"...","sequence":"...","status":1}, ...]}
-- kind: cinema, dialogue, minigame or level. status 1 playing, 6 paused.

local M = {}
local refs = require("object_refs")
local STATE_PATH = "C:\\CaitGPT\\Telemetry\\cutscene.state"
local HEARTBEAT_TICKS = 10
local SCAN_CLASS = "LevelSequencePlayer"
local PERIODIC_SCAN_TICKS = 300
local EVENT_SCAN_TICKS = 10
local EVENT_SCAN_WINDOW_TICKS = 50
local MAX_LISTED = 8
local STATUS_PLAYING, STATUS_PAUSED = 1, 6

local players = {}
local ticks_since_scan = math.huge
local ticks_since_released = math.huge
local leader_was_valid = nil
local last_written = nil
local ticks_since_write = HEARTBEAT_TICKS

local function safe(default, callback)
    local ok, value = pcall(callback)
    if ok and value ~= nil then return value end
    return default
end

local function valid(object)
    return object ~= nil and safe(false, function() return object:IsValid() end) == true
end

local function short_class(object)
    local name = safe("", function() return object:GetClass():GetFName():ToString() end)
    return type(name) == "string" and name or ""
end

local function json_string(value)
    value = tostring(value or ""):gsub('[%c"\\]', function(character)
        if character == '"' then return '\\"' end
        if character == "\\" then return "\\\\" end
        return ""
    end)
    return '"' .. value .. '"'
end

local function json_bool(value)
    if type(value) ~= "boolean" then return "null" end
    return tostring(value)
end

-- Class.member pairs whose read failed once: never tried again.
local missing = {}

-- object[name] (a property, or a method to call with `call`), or nil.
local function member(object, name, call)
    local key = short_class(object) .. "." .. name
    if missing[key] then return nil end
    local ok, value = pcall(function()
        if call then return object[name](object) end
        return object[name]
    end)
    if not ok then
        missing[key] = true
        return nil
    end
    return value
end

local function kind_of(class_name)
    if class_name:find("Cinema", 1, true) then return "cinema" end
    if class_name:find("Dialogue", 1, true) then return "dialogue" end
    if class_name:find("MiniGame", 1, true) then return "minigame" end
    return "level"
end

local function scan()
    ticks_since_scan = 0
    if type(FindAllOf) ~= "function" then return end
    local found = safe(nil, function() return FindAllOf(SCAN_CLASS) end)
    if type(found) ~= "table" then return end
    local fresh = {}
    for _, object in ipairs(found) do
        local name = safe("", function() return object:GetFullName() end)
        if valid(object) and not name:find("Default__", 1, true) then
            local class_name = short_class(object)
            local path = refs.path(object)
            if path then fresh[#fresh + 1] = {path = path, class = class_name, kind = kind_of(class_name)} end
        end
    end
    players = fresh
end

local function sequence_name(player)
    local sequence = member(player, "Sequence")
    if not valid(sequence) then return "" end
    return safe("", function() return sequence:GetFName():ToString() end)
end

local function anim_cutscene(leader_actor)
    local mesh = member(leader_actor, "Mesh")
    if not valid(mesh) then return nil end
    local anim = member(mesh, "GetAnimInstance", true)
    if not valid(anim) then return nil end
    local value = member(anim, "bPlayingCutScene")
    if type(value) == "boolean" then return value end
    return nil
end

-- The class of what the camera views, read through
-- PlayerCameraManager.ViewTarget.Target (there is no GetViewTarget UFunction).
local function view_target(leader_actor)
    local controller = member(leader_actor, "Controller")
    local camera = valid(controller) and member(controller, "PlayerCameraManager") or nil
    local view = valid(camera) and member(camera, "ViewTarget") or nil
    local target = view and safe(nil, function() return view.Target end) or nil
    if not valid(target) then return "" end
    return short_class(target)
end

-- Called every producer tick on the game thread.
function M.sample(sequence, leader_actor, source_timestamp)
    local leader = valid(leader_actor)
    ticks_since_scan = ticks_since_scan + 1
    ticks_since_released = ticks_since_released + 1
    if leader_was_valid ~= false and not leader then
        ticks_since_released = 0
    end
    leader_was_valid = leader
    if ticks_since_released == 0
        or (ticks_since_released <= EVENT_SCAN_WINDOW_TICKS and ticks_since_scan >= EVENT_SCAN_TICKS)
        or ticks_since_scan >= PERIODIC_SCAN_TICKS then
        scan()
    end

    local playing, kept = {}, {}
    for _, entry in ipairs(players) do
        local object = refs.resolve(entry.path)
        if valid(object) then
            kept[#kept + 1] = entry
            local status = nil
            if member(object, "IsPlaying", true) == true then
                status = STATUS_PLAYING
            elseif member(object, "IsPaused", true) == true then
                status = STATUS_PAUSED
            end
            if status then
                playing[#playing + 1] = {kind = entry.kind, class = entry.class,
                                         sequence = sequence_name(object), status = status}
            end
        end
    end
    players = kept
    table.sort(playing, function(a, b)
        if a.kind ~= b.kind then return a.kind < b.kind end
        return a.sequence < b.sequence
    end)
    local items = {}
    for index = 1, math.min(#playing, MAX_LISTED) do
        local item = playing[index]
        items[#items + 1] = string.format('{"kind":%s,"player_class":%s,"sequence":%s,"status":%d}',
            json_string(item.kind), json_string(item.class), json_string(item.sequence), item.status)
    end
    local anim, target = nil, ""
    if leader then
        anim = anim_cutscene(leader_actor)
        target = view_target(leader_actor)
    end
    local body = string.format('"leader":%s,"tracked":%d,"anim_cutscene":%s,"view_target":%s,"playing":[%s]',
        tostring(leader), #players, json_bool(anim), json_string(target), table.concat(items, ","))
    ticks_since_write = ticks_since_write + 1
    if body == last_written and ticks_since_write < HEARTBEAT_TICKS then return false end
    local text = string.format('{"schema_version":4,"sequence":%d,"timestamp_utc":%s,%s}\n',
        sequence or 0, json_string(source_timestamp), body)
    local file = io.open(STATE_PATH, "wb")
    if not file then return false end
    local ok = pcall(function()
        assert(file:write(text))
        assert(file:flush())
    end)
    pcall(function() file:close() end)
    if ok then
        last_written = body
        ticks_since_write = 0
    end
    return ok
end

return M
