-- Optional global navigation path producer.
--
-- This file is intentionally separate from Scripts/main.lua. The installed
-- probe remains the local line-trace producer. Enable this source only after
-- controlled validation confirms the reflected NavigationSystem API.
--
-- Input:  C:\CaitGPT\Telemetry\navigation-target.json
--         {"target_id":"...","position":{"x":0,"y":0,"z":0}}
-- Output: C:\CaitGPT\Telemetry\navigation-path.json
--         complete PathPoints only; no guessed direct vector.

local TARGET_PATH = "C:\\CaitGPT\\Telemetry\\navigation-target.json"
local OUTPUT_PATH = "C:\\CaitGPT\\Telemetry\\navigation-path.json"
local TEMP_PATH = "C:\\CaitGPT\\Telemetry\\navigation-path.json.tmp"
local STATUS_PATH = "C:\\CaitGPT\\Telemetry\\navigation-path-status.json"
local STATUS_TEMP_PATH = "C:\\CaitGPT\\Telemetry\\navigation-path-status.json.tmp"
local B0_DISCOVERY_PATH = "C:\\CaitGPT\\Telemetry\\navigation-b0-discovery.json"
local B0_DISCOVERY_TEMP_PATH = "C:\\CaitGPT\\Telemetry\\navigation-b0-discovery.json.tmp"
local AI_PATH_OBSERVATION_PATH = "C:\\CaitGPT\\Telemetry\\navigation-ai-path-observation.json"
local AI_PATH_OBSERVATION_TEMP_PATH = "C:\\CaitGPT\\Telemetry\\navigation-ai-path-observation.json.tmp"
-- The broad EndCharacter/controller scan is intentionally dormant during
-- ordinary callbacks. An operator can opt in by creating this marker with the
-- exact versioned token below; removing it stops the scan on the next tick.
local AI_PATH_OBSERVATION_ENABLE_PATH = "C:\\CaitGPT\\Telemetry\\navigation-ai-path-observation.enable"
local AI_PATH_OBSERVATION_ENABLE_TOKEN = "FF7R_AI_PATH_OBSERVATION_ENABLE_V1"
local AI_PATH_OBSERVATION_ENABLE_MAX_BYTES = 64
-- The reflected global path query is also explicit opt-in. Write the target
-- first, then create this versioned one-shot request marker.
local PATH_DIAGNOSTIC_ENABLE_PATH = "C:\\CaitGPT\\Telemetry\\navigation-path-diagnostic.enable"
local PATH_DIAGNOSTIC_ENABLE_TOKEN = "FF7R_PATH_DIAGNOSTIC_ENABLE_V1"
local PATH_DIAGNOSTIC_ENABLE_MAX_BYTES = 64
local SAMPLE_PATH = "C:\\CaitGPT\\Telemetry\\state.sample"
-- Pathfinding is materially more expensive than the local clearance probe.
-- Keep it at a low rate; the request/sequence binding prevents stale paths
-- from authorising movement while this call is between samples.
local SAMPLE_PERIOD_MS = 2000
local PRODUCER_SCHEMA_VERSION = 2
-- Keep the observer alive through a normal walk from the save spawn to town.
-- At the existing two-second cadence this is about twenty minutes, while
-- remaining bounded and far below the historical sub-second scan cadence.
local AI_PATH_OBSERVATION_MAX_ATTEMPTS = 600
local PROJECT_QUERY_EXTENT = { X = 100.0, Y = 100.0, Z = 250.0 }
local UEHelpers = require("object_refs")

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
    for key, item in pairs(value) do fields[#fields + 1] = json_escape(key) .. ":" .. json_encode(item) end
    return "{" .. table.concat(fields, ",") .. "}"
end

local function read_target()
    local file = io.open(TARGET_PATH, "rb")
    if not file then return nil end
    local text = file:read("*a")
    file:close()
    local target_id = text:match('"target_id"%s*:%s*"([^\"]+)"')
    local x = tonumber(text:match('"x"%s*:%s*([%-]*[%d%.]+)'))
    local y = tonumber(text:match('"y"%s*:%s*([%-]*[%d%.]+)'))
    local z = tonumber(text:match('"z"%s*:%s*([%-]*[%d%.]+)'))
    if not target_id or not finite(x) or not finite(y) or not finite(z) then return nil end
    return target_id, { X = x, Y = y, Z = z }
end

local function read_base_sequence()
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then return nil end
    local line = file:read("*l")
    file:close()
    local sequence = line and line:match("^(%d+)|")
    return sequence and tonumber(sequence) or nil
end

local function gameplay_position_available()
    -- A valid pawn can exist during the title/menu transition. Do not invoke
    -- Unreal's navigation query until the exporter has published a real
    -- player pose; this keeps the optional producer out of the startup path.
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then return false end
    local line = file:read("*l")
    file:close()
    if not line then return false end
    -- state.sample fields are: sequence|timestamp|paused|party_count|
    -- party|player_xyz_and_rotation|camera_rotation|... .  Match the fixed
    -- prefix so commas in the party-health field cannot be mistaken for XYZ.
    local pose = line:match("^[^|]*|[^|]*|[^|]*|[^|]*|[^|]*|([^|]+)|")
    if not pose then return false end
    local sign_x, x, sign_y, y, sign_z, z = pose:match("^([%-]*)([%d%.]+),([%-]*)([%d%.]+),([%-]*)([%d%.]+),")
    return finite(tonumber((sign_x or "") .. (x or "")))
        and finite(tonumber((sign_y or "") .. (y or "")))
        and finite(tonumber((sign_z or "") .. (z or "")))
end

local function remove_output()
    os.remove(OUTPUT_PATH)
    os.remove(TEMP_PATH)
end

local function write_status(status, sequence, reason, details)
    local record = {
        schema_version = PRODUCER_SCHEMA_VERSION,
        status = status,
        base_sequence = sequence or 0,
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        reason = reason or "",
        source = "ue4ss_navmesh_path",
    }
    if type(details) == "table" then
        for key, value in pairs(details) do record[key] = value end
    end
    local file = io.open(STATUS_TEMP_PATH, "wb")
    if not file then return end
    file:write(json_encode(record) .. "\n")
    file:flush()
    file:close()
    os.remove(STATUS_PATH)
    os.rename(STATUS_TEMP_PATH, STATUS_PATH)
end

local function write_output(payload)
    local file = io.open(TEMP_PATH, "wb")
    if not file then return end
    file:write(payload)
    file:flush()
    file:close()
    os.remove(OUTPUT_PATH)
    os.rename(TEMP_PATH, OUTPUT_PATH)
end

local function point_json(point)
    local x = safe(nil, function() return point.X end) or safe(nil, function() return point.x end)
    local y = safe(nil, function() return point.Y end) or safe(nil, function() return point.y end)
    local z = safe(nil, function() return point.Z end) or safe(nil, function() return point.z end)
    if not finite(x) or not finite(y) or not finite(z) then return nil end
    return string.format('{"x":%.6f,"y":%.6f,"z":%.6f}', x, y, z)
end

local function point_table(point)
    if point == nil then return nil end
    local x = safe(nil, function() return point.X end) or safe(nil, function() return point.x end)
    local y = safe(nil, function() return point.Y end) or safe(nil, function() return point.y end)
    local z = safe(nil, function() return point.Z end) or safe(nil, function() return point.z end)
    if not finite(x) or not finite(y) or not finite(z) then return nil end
    return { x = x, y = y, z = z }
end

local function reflection_object(paths)
    for _, path in ipairs(paths) do
        local object = safe(nil, function() return StaticFindObject(path) end)
        if object ~= nil and valid(object) then return object, path end
    end
    return nil, nil
end

local function object_name(object)
    if object == nil or not valid(object) then return nil end
    return safe(nil, function() return object:GetFullName() end)
end

local function find_all_instances(class_name)
    local record = { class_name = class_name, count = 0, objects = {} }
    local instances = safe(nil, function() return FindAllOf(class_name) end)
    if type(instances) ~= "table" then
        record.error = "FindAllOf returned no table"
        return record
    end
    for _, instance in pairs(instances) do
        local name = object_name(instance)
        if name then
            record.count = record.count + 1
            if #record.objects < 16 then record.objects[#record.objects + 1] = name end
        end
    end
    return record
end

local function write_b0_discovery(sequence)
    local classes = {
        navigation_system = { "/Script/NavigationSystem.NavigationSystemV1", "/Script/NavigationSystem.NavigationSystemBase" },
        navigation_data = { "/Script/NavigationSystem.NavigationData" },
        recast_nav_mesh = { "/Script/NavigationSystem.RecastNavMesh" },
        navigation_path = { "/Script/NavigationSystem.NavigationPath" },
        ai_controller = { "/Script/AIModule.AAIController" },
        ai_blueprint_helpers = { "/Script/AIModule.AIBlueprintHelperLibrary" },
        path_following_component = { "/Script/AIModule.PathFollowingComponent" },
        end_ai_controller = { "/Script/EndGame.EndAIController" },
        end_ai_controller_player = { "/Script/EndGame.EndAIControllerPlayerBase" },
        end_ai_controller_enemy = { "/Script/EndGame.EndAIControllerEnemyBase" },
        end_ai_move_component = { "/Script/EndGame.EndAIMoveComponent" },
        end_bt_task_move_to = { "/Script/EndGame.EndBTTask_MoveTo" },
        end_bt_task_definition_move_to = { "/Script/EndGame.EndBTTask_DefinitionMoveTo" },
    }
    local functions = {
        project_point = { "/Script/NavigationSystem.NavigationSystemV1:ProjectPointToNavigation" },
        find_path_to_location = {
            "/Script/NavigationSystem.NavigationSystemV1:FindPathToLocationSynchronously",
            "/Script/NavigationSystem.NavigationSystemBase:FindPathToLocationSynchronously",
        },
        navigation_raycast = { "/Script/NavigationSystem.NavigationSystemV1:NavigationRaycast" },
        get_ai_controller = { "/Script/AIModule.AIBlueprintHelperLibrary:GetAIController" },
        get_current_path = { "/Script/AIModule.AIBlueprintHelperLibrary:GetCurrentPath" },
        get_current_path_points = { "/Script/AIModule.AIBlueprintHelperLibrary:GetCurrentPathPoints" },
        get_current_path_index = { "/Script/AIModule.AIBlueprintHelperLibrary:GetCurrentPathIndex" },
        ai_move_to_location = { "/Script/AIModule.AAIController:MoveToLocation" },
        ai_get_path_following = { "/Script/AIModule.AAIController:GetPathFollowingComponent" },
        ai_get_move_status = { "/Script/AIModule.AAIController:GetMoveStatus" },
        ai_get_immediate_destination = { "/Script/AIModule.AAIController:GetImmediateMoveDestination" },
        path_get_destination = { "/Script/AIModule.PathFollowingComponent:GetPathDestination" },
        path_get_action_type = { "/Script/AIModule.PathFollowingComponent:GetPathActionType" },
        end_test_move_to = { "/Script/EndGame.EndAIControllerPlayerBase:TestMoveTo" },
        end_test_move_to_ai_state = { "/Script/EndGame.EndAIControllerBluePrintBase:TestMoveTo_AIState" },
    }
    local record = {
        schema_version = 1,
        source = "ue4ss_navmesh_path_plan_b0_discovery",
        base_sequence = sequence,
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        movement_sent = false,
        reflected_classes = {},
        reflected_functions = {},
        live_instances = {},
    }
    for name, paths in pairs(classes) do
        local object, path = reflection_object(paths)
        record.reflected_classes[name] = { found = object ~= nil, path = path }
    end
    for name, paths in pairs(functions) do
        local object, path = reflection_object(paths)
        record.reflected_functions[name] = { found = object ~= nil, path = path }
    end
    for _, class_name in ipairs({
        "AAIController", "AEndAIController", "AEndAIControllerEnemyBase",
        "AEndAIControllerPlayerBase", "UPathFollowingComponent", "UNavigationPath",
        "ANavigationData", "ARecastNavMesh", "UEndAIMoveComponent",
    }) do
        record.live_instances[#record.live_instances + 1] = find_all_instances(class_name)
    end
    local file = io.open(B0_DISCOVERY_TEMP_PATH, "wb")
    if not file then return end
    file:write(json_encode(record) .. "\n")
    file:flush()
    file:close()
    os.remove(B0_DISCOVERY_PATH)
    os.rename(B0_DISCOVERY_TEMP_PATH, B0_DISCOVERY_PATH)
end

local function distance_m(first, second)
    return math.sqrt((second.x - first.x) ^ 2 + (second.y - first.y) ^ 2 + (second.z - first.z) ^ 2) / 100.0
end

local function call_reflected(context, function_object, ...)
    if not valid(context) or not valid(function_object) then
        return nil, "missing_explicit_context_or_function", nil
    end
    local call_args = { ... }
    -- UE4SS documents a UFunction obtained through StaticFindObject as a
    -- callable proxy whose first argument is the UObject invocation context.
    -- Try that form first.  The UObject:CallFunction helper is deliberately
    -- only a fallback: this game/UE4SS build currently reports that helper as
    -- missing both the UFunction and context even when both objects are valid.
    local ok, result = pcall(function()
        return function_object(context, table.unpack(call_args))
    end)
    if ok and result ~= nil then return result, nil, "ufunction_call_with_context" end
    local direct_error = ok and "reflected_call_returned_nil" or tostring(result)
    local fallback_ok, fallback_result = pcall(function()
        return context:CallFunction(function_object, table.unpack(call_args))
    end)
    if fallback_ok and fallback_result ~= nil then
        return fallback_result, nil, "uobject_callfunction_fallback"
    end
    local fallback_error = fallback_ok and "reflected_call_returned_nil" or tostring(fallback_result)
    return nil, "direct=" .. direct_error .. "; fallback=" .. fallback_error, "none"
end

local function extract_points(raw_points)
    local points = {}
    if type(raw_points) == "table" then
        for _, point in ipairs(raw_points) do points[#points + 1] = point end
        return points
    end
    local for_each = safe(nil, function() return raw_points.ForEach end)
    if type(for_each) ~= "function" then return points end
    pcall(function()
        raw_points:ForEach(function(first, second)
            points[#points + 1] = second or first
        end)
    end)
    return points
end

local function measured_path_length(points)
    local total = 0.0
    for index = 2, #points do total = total + distance_m(points[index - 1], points[index]) end
    return total
end

local function write_ai_path_observation(record)
    local file = io.open(AI_PATH_OBSERVATION_TEMP_PATH, "wb")
    if not file then return end
    file:write(json_encode(record) .. "\n")
    file:flush()
    file:close()
    os.remove(AI_PATH_OBSERVATION_PATH)
    os.rename(AI_PATH_OBSERVATION_TEMP_PATH, AI_PATH_OBSERVATION_PATH)
end

local function path_bends(points)
    if #points < 3 then return false end
    local previous_heading = nil
    for index = 2, #points do
        local first = points[index - 1]
        local second = points[index]
        local dx = second.x - first.x
        local dy = second.y - first.y
        if math.abs(dx) + math.abs(dy) > 1.0 then
            local heading = math.deg(math.atan(dy, dx))
            if previous_heading ~= nil then
                local delta = math.abs(heading - previous_heading) % 360.0
                delta = math.min(delta, 360.0 - delta)
                if delta >= 5.0 then return true end
            end
            previous_heading = heading
        end
    end
    return false
end

local ai_path_observation_attempts = 0
local ai_path_observation_finished = false
local ai_path_previous_positions = {}
local ai_path_observation_request_active = false

local function append_unique_instances(destination, seen, class_name, limit)
    local objects = safe(nil, function() return FindAllOf(class_name) end)
    if type(objects) ~= "table" then return end
    for _, object in pairs(objects) do
        if #destination >= limit then return end
        if valid(object) then
            local identity = object_name(object)
            if identity and not seen[identity] then
                seen[identity] = true
                destination[#destination + 1] = object
            end
        end
    end
end

local function ai_path_candidate(actor, controller, helper_default, functions, sequence)
    local actor_id = object_name(actor)
    local controller_id = object_name(controller)
    if not actor_id or not controller_id then return nil end
    local actor_position = point_table(safe(nil, function() return actor:K2_GetActorLocation() end))
    local previous = ai_path_previous_positions[actor_id]
    local displacement_m = nil
    if actor_position and previous then
        displacement_m = distance_m(previous, actor_position)
    end
    if actor_position then ai_path_previous_positions[actor_id] = actor_position end

    local path, path_error, path_style = call_reflected(
        helper_default, functions.current_path, controller
    )
    local raw_points, points_error, points_style = call_reflected(
        helper_default, functions.current_path_points, controller
    )
    local points = {}
    if raw_points ~= nil then
        for _, raw_point in ipairs(extract_points(raw_points)) do
            local point = point_table(raw_point)
            if point then points[#points + 1] = point end
        end
    end
    local path_index, path_index_error, path_index_style = call_reflected(
        helper_default, functions.current_path_index, controller
    )
    local path_component = safe(nil, function() return controller.PathFollowingComponent end)
    if not valid(path_component) then
        path_component = safe(nil, function() return controller.PathFollowingComp end)
    end
    local destination = nil
    local destination_error = nil
    local destination_style = nil
    local action_type = nil
    if valid(path_component) and functions.path_destination then
        local raw_destination
        raw_destination, destination_error, destination_style = call_reflected(
            path_component, functions.path_destination
        )
        destination = point_table(raw_destination)
        if functions.path_action_type then
            action_type = select(1, call_reflected(path_component, functions.path_action_type))
        end
    end

    local path_valid = nil
    local path_partial = nil
    if valid(path) then
        path_valid = safe(nil, function() return path:IsValid() end)
        path_partial = safe(nil, function() return path:IsPartial() end)
    end
    local moving = displacement_m ~= nil and displacement_m >= 0.02
    return {
        actor = actor_id,
        actor_class = safe(nil, function() return actor:GetClass():GetFullName() end),
        actor_position = actor_position,
        controller = controller_id,
        controller_class = safe(nil, function() return controller:GetClass():GetFullName() end),
        path_following_component = object_name(path_component),
        path_valid = path_valid,
        path_partial = path_partial,
        path_points = points,
        path_point_count = #points,
        path_index = type(path_index) == "number" and path_index or nil,
        path_destination = destination,
        path_action_type = action_type,
        path_length_m = #points >= 2 and measured_path_length(points) or nil,
        path_bends = path_bends(points),
        displacement_since_previous_sample_m = displacement_m,
        moving = moving,
        current_path_query = { error = path_error, invocation_style = path_style },
        current_path_points_query = { error = points_error, invocation_style = points_style },
        current_path_index_query = { error = path_index_error, invocation_style = path_index_style },
        path_destination_query = { error = destination_error, invocation_style = destination_style },
        base_sequence = sequence,
    }
end

local function run_ai_path_observation()
    if ai_path_observation_finished then return end
    ai_path_observation_attempts = ai_path_observation_attempts + 1
    local sequence = read_base_sequence() or 0
    local record = {
        schema_version = 1,
        source = "ue4ss_ai_path_observation_b0_5",
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        base_sequence = sequence,
        movement_sent = false,
        ai_movement_request_sent = false,
        attempt = ai_path_observation_attempts,
        max_attempts = AI_PATH_OBSERVATION_MAX_ATTEMPTS,
        status = "observation_pending",
        reason_code = "not_observed",
        candidates = {},
    }
    if not gameplay_position_available() then
        record.status = "waiting_for_gameplay"
        record.reason_code = "player_pose_unavailable"
        if ai_path_observation_attempts >= AI_PATH_OBSERVATION_MAX_ATTEMPTS then
            record.status = "inconclusive"
            record.reason_code = "bounded_observation_completed_before_gameplay"
            ai_path_observation_finished = true
        end
        write_ai_path_observation(record)
        return
    end

    local helper_default = reflection_object({
        "/Script/AIModule.Default__AIBlueprintHelperLibrary",
    })
    local functions = {
        get_ai_controller = reflection_object({ "/Script/AIModule.AIBlueprintHelperLibrary:GetAIController" }),
        current_path = reflection_object({ "/Script/AIModule.AIBlueprintHelperLibrary:GetCurrentPath" }),
        current_path_points = reflection_object({ "/Script/AIModule.AIBlueprintHelperLibrary:GetCurrentPathPoints" }),
        current_path_index = reflection_object({ "/Script/AIModule.AIBlueprintHelperLibrary:GetCurrentPathIndex" }),
        path_destination = reflection_object({ "/Script/AIModule.PathFollowingComponent:GetPathDestination" }),
        path_action_type = reflection_object({ "/Script/AIModule.PathFollowingComponent:GetPathActionType" }),
    }
    record.reflection = {
        helper_default = helper_default ~= nil,
        get_ai_controller = functions.get_ai_controller ~= nil,
        current_path = functions.current_path ~= nil,
        current_path_points = functions.current_path_points ~= nil,
        current_path_index = functions.current_path_index ~= nil,
        path_destination = functions.path_destination ~= nil,
        path_action_type = functions.path_action_type ~= nil,
    }
    if not valid(helper_default) or not valid(functions.get_ai_controller) or
       not valid(functions.current_path_points) or not valid(functions.current_path_index) then
        record.status = "unsupported"
        record.reason_code = "required_read_only_path_function_unavailable"
        ai_path_observation_finished = true
        write_ai_path_observation(record)
        return
    end

    local actors = {}
    local seen = {}
    append_unique_instances(actors, seen, "EndCharacter", 96)
    local controllers = {}
    local controller_ids = {}
    append_unique_instances(controllers, controller_ids, "EndAIControllerEnemyBase", 96)
    append_unique_instances(controllers, controller_ids, "EndAIController", 96)
    local controller_seen = {}
    for _, actor in ipairs(actors) do
        -- Always resolve a controller through the reflected helper.  Treating
        -- an actor with a Pawn property as its own controller produces a
        -- plausible-looking but incorrect controller identity and can hide
        -- the actual PathFollowingComponent owner.
        local controller = select(1, call_reflected(helper_default, functions.get_ai_controller, actor))
        if valid(controller) then
            local identity = object_name(controller)
            if identity and not controller_seen[identity] then
                local candidate = ai_path_candidate(actor, controller, helper_default, functions, sequence)
                if candidate then record.candidates[#record.candidates + 1] = candidate end
                controller_seen[identity] = true
            end
        end
    end
    for _, controller in ipairs(controllers) do
        if valid(controller) then
            local identity = object_name(controller)
            if identity and not controller_seen[identity] then
                local pawn = safe(nil, function() return controller.Pawn end)
                if not valid(pawn) then
                    pawn = controller
                end
                local candidate = ai_path_candidate(pawn, controller, helper_default, functions, sequence)
                if candidate then record.candidates[#record.candidates + 1] = candidate end
                controller_seen[identity] = true
            end
        end
    end

    local path_found = false
    local moving_path_found = false
    local bent_path_found = false
    for _, candidate in ipairs(record.candidates) do
        if candidate.path_point_count >= 2 and candidate.path_valid ~= false and candidate.path_partial ~= true then
            path_found = true
            bent_path_found = bent_path_found or candidate.path_bends
            moving_path_found = moving_path_found or candidate.moving
        end
    end
    record.candidate_count = #record.candidates
    record.path_observed = path_found
    record.moving_path_observed = moving_path_found
    record.bent_path_observed = bent_path_found
    if moving_path_found then
        record.status = "success"
        record.reason_code = bent_path_found and "moving_bent_path_observed" or "moving_path_observed"
        ai_path_observation_finished = true
    elseif path_found then
        record.status = "path_present_not_observed_moving"
        record.reason_code = "current_path_present_but_motion_not_yet_confirmed"
    elseif #record.candidates > 0 then
        record.status = "no_active_path"
        record.reason_code = "controllers_found_without_complete_path"
    else
        record.status = "no_instances"
        record.reason_code = "no_loaded_ai_character_or_controller_instances"
    end
    if ai_path_observation_attempts >= AI_PATH_OBSERVATION_MAX_ATTEMPTS then
        ai_path_observation_finished = true
        if record.status ~= "success" then
            record.status = "inconclusive"
            record.reason_code = "bounded_observation_completed_without_moving_path"
        end
    end
    write_ai_path_observation(record)
end

local function read_exact_request_marker(path, expected_token, maximum_bytes)
    local ok, file = pcall(io.open, path, "rb")
    if not ok or not file then return false end
    -- Read at most one byte beyond the documented cap. Exact token matching
    -- rejects empty, malformed, newline-terminated, and oversized requests.
    local read_ok, actual_token = pcall(function()
        return file:read(maximum_bytes + 1)
    end)
    pcall(function() file:close() end)
    return read_ok and actual_token == expected_token
end

local function read_ai_path_observation_request()
    return read_exact_request_marker(
        AI_PATH_OBSERVATION_ENABLE_PATH,
        AI_PATH_OBSERVATION_ENABLE_TOKEN,
        AI_PATH_OBSERVATION_ENABLE_MAX_BYTES
    )
end

local function read_path_diagnostic_request()
    return read_exact_request_marker(
        PATH_DIAGNOSTIC_ENABLE_PATH,
        PATH_DIAGNOSTIC_ENABLE_TOKEN,
        PATH_DIAGNOSTIC_ENABLE_MAX_BYTES
    )
end

local function write_ai_path_observation_stopped()
    write_ai_path_observation({
        schema_version = 1,
        source = "ue4ss_ai_path_observation_b0_5",
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        base_sequence = read_base_sequence() or 0,
        movement_sent = false,
        ai_movement_request_sent = false,
        attempt = ai_path_observation_attempts,
        max_attempts = AI_PATH_OBSERVATION_MAX_ATTEMPTS,
        status = "stopped",
        reason_code = "operator_request_removed_or_invalid",
        candidates = {},
    })
end

local function run_ai_path_observation_if_requested()
    if not read_ai_path_observation_request() then
        if ai_path_observation_request_active and not ai_path_observation_finished then
            write_ai_path_observation_stopped()
        end
        ai_path_observation_request_active = false
        ai_path_observation_attempts = 0
        ai_path_observation_finished = false
        ai_path_previous_positions = {}
        return
    end

    if not ai_path_observation_request_active then
        ai_path_observation_request_active = true
        ai_path_observation_attempts = 0
        ai_path_observation_finished = false
        ai_path_previous_positions = {}
    end
    -- Keep the request latched as active after success or the attempt cap.
    -- The operator must remove the marker before a later request can re-arm,
    -- so a forgotten marker cannot start another bounded scan in this Lua
    -- session.
    if not ai_path_observation_finished then
        run_ai_path_observation()
    end
end

local function write_path_legacy()
    local target_id, target = read_target()
    local sequence = read_base_sequence()
    if not sequence then remove_output(); return end
    if not target_id then
        remove_output()
        write_status("waiting_for_target", sequence, "navigation-target.json is missing or invalid")
        return
    end
    if not gameplay_position_available() then
        remove_output()
        write_status("waiting_for_gameplay", sequence, "exporter has not published a player pose")
        return
    end
    local controller = safe(nil, function() return UEHelpers.GetPlayerController() end)
    local pawn = valid(controller) and safe(nil, function() return controller.Pawn end) or nil
    local world = valid(pawn) and safe(nil, function() return pawn:GetWorld() end) or nil
    if not valid(pawn) or not valid(world) then
        remove_output()
        write_status("waiting_for_gameplay", sequence, "player pawn or world is unavailable")
        return
    end
    local function find_first(paths)
        for _, path in ipairs(paths) do
            local object = safe(nil, function() return StaticFindObject(path) end)
            if valid(object) then return object, path end
        end
        return nil, nil
    end
    local path_function, path_name = find_first({
        "/Script/NavigationSystem.NavigationSystemV1:FindPathToLocationSynchronously",
        "/Script/NavigationSystem.NavigationSystemBase:FindPathToLocationSynchronously",
    })
    if not valid(path_function) then
        remove_output()
        write_status("unsupported", sequence, "FindPathToLocationSynchronously was not reflected", {
            navigation_system_module = "NavigationSystem",
        })
        return
    end
    -- UE4SS exposes a reflected UFunction as a callable proxy.  Keep this
    -- call strictly after gameplay/pose readiness; static navigation queries
    -- against the title-screen world are not safe in this game.
    -- Retired prototype call intentionally remains inert; live execution uses
    -- write_path_diagnostic below with an explicit UObject context.
    local path = nil
    if not valid(path) then
        remove_output()
        write_status("no_path", sequence, "reflected path call returned no UNavigationPath", {target_id = target_id, function_path = path_name})
        return
    end
    if safe(true, function() return path:IsPartial() end) then
        remove_output()
        write_status("partial_path", sequence, "Unreal returned a partial path", {target_id = target_id, function_path = path_name})
        return
    end
    local raw_points = safe(nil, function() return path.PathPoints end)
    if type(raw_points) ~= "table" or #raw_points < 2 then
        remove_output()
        write_status("no_path", sequence, "complete path contained fewer than two points", {target_id = target_id, function_path = path_name})
        return
    end
    local points = {}
    for _, point in ipairs(raw_points) do
        local encoded = point_json(point)
        if not encoded then
            remove_output()
            write_status("invalid_path", sequence, "path contained a non-finite point", {target_id = target_id, function_path = path_name})
            return
        end
        points[#points + 1] = encoded
    end
    write_output(string.format('{"schema_version":1,"base_sequence":%d,"timestamp_utc":%s,"source":"ue4ss_navmesh_path","path_source":"FindPathToLocationSynchronously","target_id":%s,"path_complete":true,"navmesh_path":[%s]}\n', sequence, json_escape(os.date("!%Y-%m-%dT%H:%M:%SZ")), json_escape(target_id), table.concat(points, ",")))
    write_status("path_ready", sequence, "complete path published", {target_id = target_id, point_count = #points, function_path = path_name})
end

-- Plan A diagnostic implementation. This keeps the original prototype above
-- available for comparison while the loop below uses only the explicit
-- UObject-context form and publishes richer fail-closed evidence.
local function write_path_diagnostic()
    local target_id, target = read_target()
    local sequence = read_base_sequence()
    if not sequence then
        remove_output()
        write_status("waiting_for_telemetry", 0, "state.sample is missing or has no sequence", { reason_code = "telemetry_unavailable" })
        return
    end
    if not target_id then
        remove_output()
        write_status("waiting_for_target", sequence, "navigation-target.json is missing or invalid", { reason_code = "target_invalid" })
        return
    end
    if not gameplay_position_available() then
        remove_output()
        write_status("waiting_for_gameplay", sequence, "exporter has not published a player pose", { reason_code = "player_pose_unavailable" })
        return
    end
    local controller = safe(nil, function() return UEHelpers.GetPlayerController() end)
    local pawn = valid(controller) and safe(nil, function() return controller.Pawn end) or nil
    local world = valid(pawn) and safe(nil, function() return pawn:GetWorld() end) or nil
    if not valid(pawn) or not valid(world) then
        remove_output()
        write_status("waiting_for_gameplay", sequence, "player pawn or world is unavailable", { reason_code = "world_or_pawn_unavailable" })
        return
    end

    local discovery_file = io.open(B0_DISCOVERY_PATH, "rb")
    if discovery_file then
        discovery_file:close()
    else
        pcall(write_b0_discovery, sequence)
    end

    local query_started = os.clock()
    local function find_first_reflection(paths)
        for _, path in ipairs(paths) do
            local object = safe(nil, function() return StaticFindObject(path) end)
            if valid(object) then return object, path end
        end
        return nil, nil
    end
    local navigation_default, navigation_default_path = find_first_reflection({
        "/Script/NavigationSystem.Default__NavigationSystemV1",
        "/Script/NavigationSystem.Default__NavigationSystemBase",
    })
    local navigation_class, navigation_class_path = find_first_reflection({
        "/Script/NavigationSystem.NavigationSystemV1",
        "/Script/NavigationSystem.NavigationSystemBase",
    })
    local project_function, project_function_path = find_first_reflection({
        "/Script/NavigationSystem.NavigationSystemV1:ProjectPointToNavigation",
        "/Script/NavigationSystem.NavigationSystemBase:ProjectPointToNavigation",
    })
    local path_function, path_function_path = find_first_reflection({
        "/Script/NavigationSystem.NavigationSystemV1:FindPathToLocationSynchronously",
        "/Script/NavigationSystem.NavigationSystemBase:FindPathToLocationSynchronously",
    })
    local raycast_function, raycast_function_path = find_first_reflection({
        "/Script/NavigationSystem.NavigationSystemV1:NavigationRaycast",
        "/Script/NavigationSystem.NavigationSystemBase:NavigationRaycast",
    })
    local reflection = {
        navigation_context = navigation_default_path,
        navigation_class = navigation_class_path,
        project_point = project_function_path,
        find_path = path_function_path,
        navigation_raycast = raycast_function_path,
    }
    if not valid(navigation_default) then
        remove_output()
        write_status("unsupported", sequence, "required Unreal navigation reflection is unavailable", {
            reason_code = "reflected_function_unavailable",
            reflection = reflection,
            query_duration_ms = (os.clock() - query_started) * 1000.0,
        })
        return
    end
    if not valid(project_function) then
        remove_output()
        write_status("unsupported", sequence, "ProjectPointToNavigation was not reflected", {
            reason_code = "project_point_reflection_unavailable",
            reflection = reflection,
            query_duration_ms = (os.clock() - query_started) * 1000.0,
        })
        return
    end

    -- StaticFindObject returns an unbound UFunction. UE4SS requires the
    -- explicit UObject invocation context before the actual Unreal parameters.
    -- Rebirth's working telemetry probes use the Blueprint-compatible K2
    -- accessor; the generic GetActorLocation binding can be a null UObject
    -- wrapper even while the exporter has a valid player pose.
    local original_start_raw = safe(nil, function() return pawn:K2_GetActorLocation() end)
    local original_start = point_table(original_start_raw)
    local original_goal = point_table(target)
    if not original_start or not original_goal then
        remove_output()
        write_status("invocation_failed", sequence, "Cloud or target position was not a finite FVector", {
            reason_code = "invalid_query_position",
            reflection = reflection,
        })
        return
    end

    local projected_start_raw, projected_start_error, projected_start_style = call_reflected(
        navigation_default, project_function, world, original_start_raw, nil, nil, PROJECT_QUERY_EXTENT
    )
    local projected_start = point_table(projected_start_raw)
    local projected_goal_raw, projected_goal_error, projected_goal_style = call_reflected(
        navigation_default, project_function, world, target, nil, nil, PROJECT_QUERY_EXTENT
    )
    local projected_goal = point_table(projected_goal_raw)
    local projection_details = {
        original_start = original_start,
        original_goal = original_goal,
        projected_start = projected_start,
        projected_goal = projected_goal,
        projection_extent = { x = PROJECT_QUERY_EXTENT.X, y = PROJECT_QUERY_EXTENT.Y, z = PROJECT_QUERY_EXTENT.Z },
        projection_errors = { start = projected_start_error, goal = projected_goal_error },
        invocation_style = { start = projected_start_style, goal = projected_goal_style },
    }
    if not projected_start then
        remove_output()
        write_status("start_projection_failed", sequence, "ProjectPointToNavigation did not return a finite start point", {
            reason_code = "start_projection_failed",
            reflection = reflection,
            diagnostics = projection_details,
            query_duration_ms = (os.clock() - query_started) * 1000.0,
        })
        return
    end
    if not projected_goal then
        remove_output()
        write_status("target_projection_failed", sequence, "ProjectPointToNavigation did not return a finite target point", {
            reason_code = "target_projection_failed",
            reflection = reflection,
            diagnostics = projection_details,
            query_duration_ms = (os.clock() - query_started) * 1000.0,
        })
        return
    end

    local direct_path = {
        method = "NavigationRaycast",
        function_path = raycast_function_path,
        status = "reflection_unavailable",
        reason_code = "reflected_function_unavailable",
    }
    if valid(raycast_function) then
        local raycast_result, raycast_error, raycast_style = call_reflected(
            navigation_default,
            raycast_function,
            world,
            projected_start_raw,
            projected_goal_raw,
            { X = 0.0, Y = 0.0, Z = 0.0 },
            nil,
            pawn
        )
        if type(raycast_result) == "boolean" then
            direct_path.status = raycast_result and "blocked" or "clear"
            direct_path.reason_code = raycast_result and "navigation_hit" or "navigation_clear"
        else
            direct_path.status = "invocation_failed"
            direct_path.reason_code = "navigation_raycast_invocation_failed"
            direct_path.error = raycast_error or "NavigationRaycast returned no boolean"
        end
        direct_path.invocation_style = raycast_style
    end

    if not valid(path_function) then
        remove_output()
        write_status("unsupported", sequence, "FindPathToLocationSynchronously was not reflected", {
            reason_code = "find_path_reflection_unavailable",
            target_id = target_id,
            reflection = reflection,
            diagnostics = projection_details,
            direct_path = direct_path,
            query_duration_ms = (os.clock() - query_started) * 1000.0,
        })
        return
    end

    local path_started = os.clock()
    local path, path_error, path_invocation_style = call_reflected(
        navigation_default, path_function, world, projected_start_raw, projected_goal_raw, pawn, nil
    )
    local path_duration_ms = (os.clock() - path_started) * 1000.0
    if not valid(path) then
        remove_output()
        write_status("invocation_failed", sequence, "FindPathToLocationSynchronously returned no valid UNavigationPath", {
            reason_code = "find_path_invocation_failed",
            target_id = target_id,
            reflection = reflection,
            diagnostics = projection_details,
            direct_path = direct_path,
            error = path_error,
            invocation_style = path_invocation_style,
            query_duration_ms = (os.clock() - query_started) * 1000.0,
            path_query_duration_ms = path_duration_ms,
        })
        return
    end
    local path_valid = safe(nil, function() return path:IsValid() end)
    local path_partial = safe(nil, function() return path:IsPartial() end)
    if path_valid ~= true or path_partial ~= false then
        remove_output()
        write_status(path_partial == true and "partial_path" or "invocation_failed", sequence,
            path_partial == true and "Unreal returned a partial path" or "UNavigationPath validity was not confirmed", {
                reason_code = path_partial == true and "partial_path" or "path_validity_unknown",
                target_id = target_id,
                reflection = reflection,
                diagnostics = projection_details,
                direct_path = direct_path,
                partial = path_partial,
                path_valid = path_valid,
                invocation_style = path_invocation_style,
                query_duration_ms = (os.clock() - query_started) * 1000.0,
                path_query_duration_ms = path_duration_ms,
            })
        return
    end

    local raw_points = safe(nil, function() return path.PathPoints end)
    local raw_point_list = extract_points(raw_points)
    if #raw_point_list < 2 then
        remove_output()
        write_status("no_path", sequence, "complete path contained fewer than two points", {
            reason_code = "no_path_points",
            target_id = target_id,
            reflection = reflection,
            diagnostics = projection_details,
            direct_path = direct_path,
            query_duration_ms = (os.clock() - query_started) * 1000.0,
            path_query_duration_ms = path_duration_ms,
            invocation_style = path_invocation_style,
        })
        return
    end
    local points = {}
    for _, raw_point in ipairs(raw_point_list) do
        local point = point_table(raw_point)
        if not point then
            remove_output()
            write_status("invalid_path", sequence, "path contained a non-finite point", {
                reason_code = "non_finite_path_point",
                target_id = target_id,
                reflection = reflection,
                diagnostics = projection_details,
                direct_path = direct_path,
                query_duration_ms = (os.clock() - query_started) * 1000.0,
                path_query_duration_ms = path_duration_ms,
                invocation_style = path_invocation_style,
            })
            return
        end
        points[#points + 1] = point
    end
    if distance_m(points[1], projected_start) > 500.0 or distance_m(points[#points], projected_goal) > 500.0 then
        remove_output()
        write_status("invalid_path", sequence, "path endpoints do not bind to projected query positions", {
            reason_code = "path_endpoint_mismatch",
            target_id = target_id,
            reflection = reflection,
            diagnostics = projection_details,
            direct_path = direct_path,
            point_count = #points,
            query_duration_ms = (os.clock() - query_started) * 1000.0,
            path_query_duration_ms = path_duration_ms,
            invocation_style = path_invocation_style,
        })
        return
    end

    local native_length = safe(nil, function() return path:GetPathLength() end)
    local native_cost = safe(nil, function() return path:GetCost() end)
    local path_length_m = finite(native_length) and native_length / 100.0 or measured_path_length(points)
    local path_record = {
        schema_version = 1,
        producer_schema_version = PRODUCER_SCHEMA_VERSION,
        base_sequence = sequence,
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        source = "ue4ss_navmesh_path",
        path_source = "FindPathToLocationSynchronously",
        target_id = target_id,
        path_complete = true,
        partial = false,
        point_count = #points,
        path_length_m = path_length_m,
        path_cost = finite(native_cost) and native_cost or nil,
        query_duration_ms = (os.clock() - query_started) * 1000.0,
        path_query_duration_ms = path_duration_ms,
        invocation_style = path_invocation_style,
        diagnostics = projection_details,
        direct_path = direct_path,
        navmesh_path = points,
    }
    write_output(json_encode(path_record) .. "\n")
    write_status("path_ready", sequence, "complete path published", {
        reason_code = "complete_path",
        target_id = target_id,
        reflection = reflection,
        diagnostics = projection_details,
        direct_path = direct_path,
        point_count = #points,
        path_length_m = path_length_m,
        path_cost = finite(native_cost) and native_cost or nil,
        partial = false,
        path_valid = true,
        query_duration_ms = (os.clock() - query_started) * 1000.0,
        path_query_duration_ms = path_duration_ms,
    })
end

local function run_path_diagnostic()
    local ok, error_message = pcall(write_path_diagnostic)
    if not ok then
        local sequence = read_base_sequence() or 0
        remove_output()
        write_status("invocation_failed", sequence, tostring(error_message), {
            reason_code = "diagnostic_exception",
        })
    end
end

local path_diagnostic_request_active = false
local path_diagnostic_attempted = false
local path_diagnostic_disabled_status_written = false

local function disable_path_diagnostic(status, reason_code, reason)
    remove_output()
    write_status(status, read_base_sequence() or 0, reason, {
        reason_code = reason_code,
        request_marker = "navigation-path-diagnostic.enable",
    })
end

local function run_path_diagnostic_if_requested()
    if read_path_diagnostic_request() then
        if not path_diagnostic_request_active then
            path_diagnostic_request_active = true
            path_diagnostic_attempted = false
            path_diagnostic_disabled_status_written = false
        end
        if not path_diagnostic_attempted then
            -- One bounded invocation per marker insertion. Keep it latched
            -- until the operator removes or invalidates the request file.
            path_diagnostic_attempted = true
            run_path_diagnostic()
        end
        return
    end

    if path_diagnostic_request_active then
        path_diagnostic_request_active = false
        path_diagnostic_attempted = false
        path_diagnostic_disabled_status_written = true
        disable_path_diagnostic(
            "stopped",
            "diagnostic_request_removed_or_invalid",
            "The operator request marker was removed or invalidated."
        )
        return
    end

    if not path_diagnostic_disabled_status_written then
        path_diagnostic_disabled_status_written = true
        disable_path_diagnostic(
            "disabled",
            "diagnostic_request_not_enabled",
            "The explicit diagnostic request marker is missing or invalid."
        )
    end
end

-- Called only by the paired native persistent game-thread scheduler.
function FF7RReadOnlyTick()
    run_ai_path_observation_if_requested()
    run_path_diagnostic_if_requested()
end
