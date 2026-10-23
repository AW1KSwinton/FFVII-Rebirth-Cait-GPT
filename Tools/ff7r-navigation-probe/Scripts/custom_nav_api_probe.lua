-- Read-only EndNavAPI reflection/projection diagnostic.
--
-- This probe is deliberately separate from the production navigation sidecar.
-- It runs only when the explicit marker exists, consumes that marker before
-- querying, and writes navigation-custom-api.json. It never sends input,
-- invokes AI movement, or writes navigation-path.json.

local MARKER_PATH = "C:\\CaitGPT\\Telemetry\\navigation-custom-api.enable"
local OUTPUT_PATH = "C:\\CaitGPT\\Telemetry\\navigation-custom-api.json"
local TEMP_PATH = OUTPUT_PATH .. ".tmp"
local SAMPLE_PATH = "C:\\CaitGPT\\Telemetry\\state.sample"
local SAMPLE_PERIOD_MS = 1000
local MAX_PROPERTY_COUNT = 64
-- A prior settled-gameplay run reached the first reflected projection call and
-- never returned. Keep the marker useful for static inventory/result emission,
-- but require a separately audited source change before any future live call.
local LIVE_REFLECTED_PROJECTION_CALLS_ENABLED = false

local UEHelpers = require("UEHelpers")

local function safe(default, callback)
    local ok, value = pcall(callback)
    if ok and value ~= nil then return value end
    return default
end

local function valid(object)
    return safe(false, function() return object ~= nil and object:IsValid() end)
end

local function finite(value)
    return type(value) == "number" and value == value and value > -1.0e30 and value < 1.0e30
end

local function point_table(point)
    if point == nil then return nil end
    local x = safe(nil, function() return point.X end) or safe(nil, function() return point.x end)
    local y = safe(nil, function() return point.Y end) or safe(nil, function() return point.y end)
    local z = safe(nil, function() return point.Z end) or safe(nil, function() return point.z end)
    if not finite(x) or not finite(y) or not finite(z) then return nil end
    return { x = x, y = y, z = z }
end

local function json_escape(value)
    local text = tostring(value or "")
    text = text:gsub("\\", "\\\\"):gsub('"', '\\"')
    return '"' .. text .. '"'
end

local function json_encode(value)
    local value_type = type(value)
    if value == nil then return "null" end
    if value_type == "string" then return json_escape(value) end
    if value_type == "boolean" then return value and "true" or "false" end
    if value_type == "number" then return finite(value) and tostring(value) or "null" end
    if value_type ~= "table" then return json_escape(tostring(value)) end
    local is_array, maximum = true, 0
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
    return valid(object) and safe(nil, function() return object:GetFullName() end) or nil
end

local function class_name(object)
    if not valid(object) then return nil end
    return safe(nil, function() return object:GetClass():GetFullName() end)
end

local function read_sequence()
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then return nil end
    local line = file:read("*l")
    file:close()
    local sequence = line and line:match("^(%d+)|")
    return sequence and tonumber(sequence) or nil
end

local function read_player_position()
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then return nil end
    local line = file:read("*l")
    file:close()
    if not line then return nil end
    -- state.sample uses a fixed pipe-delimited player-pose field:
    -- x,y,z,pitch,yaw,roll. Reading this sidecar avoids UEHelpers' broad
    -- FindAllOf-based player-controller lookup, which can be unavailable in a
    -- fresh Lua state even while the telemetry exporter is live.
    local pose = line:match("^[^|]*|[^|]*|[^|]*|[^|]*|[^|]*|([^|]+)|")
    if not pose then return nil end
    local x, y, z = pose:match("^([%-]*[%d%.]+),([%-]*[%d%.]+),([%-]*[%d%.]+),")
    x, y, z = tonumber(x), tonumber(y), tonumber(z)
    if not finite(x) or not finite(y) or not finite(z) then return nil end
    return { x = x, y = y, z = z }
end

local function write_record(record)
    local ok, payload = pcall(function() return json_encode(record) .. "\n" end)
    if not ok then
        payload = '{"schema_version":1,"source":"ue4ss_end_nav_api_projection_diagnostic",'
            .. '"status":"probe_error","reason_code":"json_serialization_failed",'
            .. '"error":' .. json_escape(payload) .. '}\n'
    end
    local file, open_error = io.open(TEMP_PATH, "wb")
    if not file then
        print("FF7RCustomNavAPIProbe output temp open failed: " .. tostring(open_error))
        return false
    end
    local write_ok, write_error = file:write(payload)
    file:flush()
    file:close()
    if not write_ok then
        print("FF7RCustomNavAPIProbe output temp write failed: " .. tostring(write_error))
        os.remove(TEMP_PATH)
        return false
    end
    os.remove(OUTPUT_PATH)
    local rename_ok, rename_error = os.rename(TEMP_PATH, OUTPUT_PATH)
    if not rename_ok then
        print("FF7RCustomNavAPIProbe output rename failed: " .. tostring(rename_error))
        os.remove(TEMP_PATH)
        return false
    end
    return true
end

local function reflection_object(path)
    local object = safe(nil, function() return StaticFindObject(path) end)
    return valid(object) and object or nil
end

local function function_metadata(path)
    -- Do not call UFunction::GetParmsSize/ForEachProperty here.  On this
    -- shipping UE4SS build a StaticFindObject UFunction proxy can pass the
    -- IsValid check while still throwing "UObject instance is nullptr" from
    -- those metadata helpers.  The generated header signatures below are the
    -- authoritative static layout; the live probe should reserve its risk
    -- budget for the actual read-only invocation.
    local raw_object = safe(nil, function() return StaticFindObject(path) end)
    local function_object = reflection_object(path)
    local result = {
        path = path,
        raw_object_non_nil = raw_object ~= nil,
        found = function_object ~= nil,
        full_name = object_name(function_object),
        class = class_name(function_object),
        parameter_metadata = "not_called_on_live_ufunction_proxy",
    }
    return result, function_object
end

local function invoke(context, function_object, ...)
    if not valid(context) or not valid(function_object) then
        return nil, "missing_context_or_function", "none"
    end
    local args = { ... }
    local ok, value = pcall(function()
        return function_object(context, table.unpack(args))
    end)
    if ok then return value, nil, "ufunction_call_with_context" end
    local direct_error = tostring(value)
    local fallback_ok, fallback_value = pcall(function()
        return context:CallFunction(function_object, table.unpack(args))
    end)
    if fallback_ok then return fallback_value, nil, "context_call_function" end
    return nil, direct_error .. " | CallFunction fallback: " .. tostring(fallback_value), "failed"
end

local function find_first(paths)
    for _, path in ipairs(paths) do
        local object = reflection_object(path)
        if object then return object, path end
    end
    return nil, nil
end

local function read_agent_properties(api, get_agent_function)
    local records = {}
    -- EEndNavLayerType is an enum byte. The generated dump establishes the
    -- parameter type; probing the first two enum values is bounded and
    -- read-only. Unknown values are recorded as invocation failures.
    for layer = 0, 1 do
        local result, error_text, style = invoke(api, get_agent_function, layer)
        records[#records + 1] = {
            layer_value = layer,
            invocation_style = style,
            error = error_text,
            value_type = type(result),
            value = result and tostring(result) or nil,
        }
    end
    return records
end

local function run_once()
    local marker = io.open(MARKER_PATH, "rb")
    if not marker then return end
    marker:close()
    os.remove(MARKER_PATH)
    print("FF7RCustomNavAPIProbe marker consumed")

    local started = os.clock()
    local sequence = read_sequence() or 0
    local current = read_player_position()

    local api, api_path = find_first({
        "/Script/EndGame.Default__EndNavAPI",
        "/Script/EndGame.EndNavAPI",
    })
    local get_agent_metadata, get_agent_function = function_metadata(
        "/Script/EndGame.EndNavAPI:GetAgentPropety"
    )
    local nearest_metadata, nearest_function = function_metadata(
        "/Script/EndGame.EndNavAPI:FindNearestNavMeshPoint"
    )
    local nearest_agent_metadata, nearest_agent_function = function_metadata(
        "/Script/EndGame.EndNavAPI:FindNearestNavMeshPointAgent"
    )
    local reflection = {
        api = { found = api ~= nil, path = api_path, full_name = object_name(api), class = class_name(api) },
        get_agent_property = get_agent_metadata,
        find_nearest = nearest_metadata,
        find_nearest_agent = nearest_agent_metadata,
        generated_header_signature = {
            get_agent_property = "FNavAgentProperties GetAgentPropety(EEndNavLayerType InLayerType)",
            find_nearest = "bool FindNearestNavMeshPoint(FVector TargetLocation, FVector& OutLocation, FName InModifierName, FVector SearchRange, EEndQueryFilterType FilterType)",
            find_nearest_agent = "bool FindNearestNavMeshPointAgent(FVector TargetLocation, FVector& OutLocation, FName InModifierName, FNavAgentProperties InAgentProperty, FVector SearchRange, EEndQueryFilterType FilterType)",
            filter_default_value = 0,
            out_location_is_by_reference = true,
        },
    }

    local record = {
        schema_version = 1,
        source = "ue4ss_end_nav_api_projection_diagnostic",
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        base_sequence = sequence,
        movement_sent = false,
        ai_movement_request_sent = false,
        api_context_available = valid(api),
        player_position = current,
        reflection = reflection,
        agent_properties = {},
        queries = {},
        status = "reflection_only",
        reason_code = "not_run",
    }

    if not api or not current or not nearest_function then
        record.status = "unsupported"
        record.reason_code = not api and "end_nav_api_context_unavailable"
            or not current and "player_position_unavailable"
            or "find_nearest_reflection_unavailable"
        record.query_duration_ms = (os.clock() - started) * 1000.0
        print("FF7RCustomNavAPIProbe writing unsupported record: " .. tostring(record.reason_code))
        write_record(record)
        return
    end

    if not LIVE_REFLECTED_PROJECTION_CALLS_ENABLED then
        record.status = "unsupported"
        record.reason_code = "reflected_projection_invocation_disabled_after_nonreturning_call"
        record.live_invocation = {
            enabled = false,
            reason = "FindNearestNavMeshPoint did not return in the final settled-gameplay attempt",
            last_checkpoint = "query_begin=current",
        }
        write_record(record)
        return
    end

    record.agent_properties = read_agent_properties(api, get_agent_function)
    -- Keep the first live call on the simpler reflected signature. UE4SS's
    -- FName helper returns a proxy that is not safely marshalable into this
    -- shipping UFunction; the literal is the value accepted by the existing
    -- reflected probes and avoids a push_strproperty failure.
    local modifier_name = "None"
    local search_range = { X = 100.0, Y = 100.0, Z = 250.0 }
    local candidates = {
        { id = "current", point = current, expected = "reachable_current" },
        { id = "nearby_x_5m", point = { x = current.x + 500.0, y = current.y, z = current.z }, expected = "nearby_walkable_or_projectable" },
        { id = "high_invalid_20m", point = { x = current.x, y = current.y, z = current.z + 2000.0 }, expected = "invalid_or_unprojectable" },
    }
    for _, candidate in ipairs(candidates) do
        local target = { X = candidate.point.x, Y = candidate.point.y, Z = candidate.point.z }
        local out_location = { X = 0.0, Y = 0.0, Z = 0.0 }
        local result, error_text, style = invoke(api, nearest_function, target, out_location, modifier_name, search_range, 0)
        local projected = point_table(out_location)
        record.queries[#record.queries + 1] = {
            id = candidate.id,
            expected = candidate.expected,
            function_path = "/Script/EndGame.EndNavAPI:FindNearestNavMeshPoint",
            target = candidate.point,
            out_location_after_call = projected,
            success = type(result) == "boolean" and result or nil,
            returned_type = type(result),
            error = error_text,
            invocation_style = style,
            filter_type = 0,
            modifier_name = "None",
            search_range = { x = 100.0, y = 100.0, z = 250.0 },
        }
        -- The agent overload remains statically inventoried, but is deferred
        -- until the non-agent FName/out-parameter marshalling is proven. A
        -- failed agent struct conversion must not hide useful projection
        -- evidence from the simpler call.
    end
    record.status = "completed"
    record.reason_code = "projection_queries_completed"
    record.query_duration_ms = (os.clock() - started) * 1000.0
    print("FF7RCustomNavAPIProbe writing completed record")
    write_record(record)
end

local function guarded_run()
    local ok, error_text = pcall(run_once)
    if ok then return end
    -- The marker has already been consumed.  Preserve an exact structured
    -- failure rather than silently losing the experiment if a UE4SS proxy or
    -- Lua marshalling edge case escapes the inner guards.
    print("FF7RCustomNavAPIProbe writing unhandled error record")
    write_record({
        schema_version = 1,
        source = "ue4ss_end_nav_api_projection_diagnostic",
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        movement_sent = false,
        ai_movement_request_sent = false,
        status = "probe_error",
        reason_code = "unhandled_probe_error",
        error = tostring(error_text),
    })
end

-- Called only by the paired native persistent game-thread scheduler.
function FF7RReadOnlyTick()
    guarded_run()
end
