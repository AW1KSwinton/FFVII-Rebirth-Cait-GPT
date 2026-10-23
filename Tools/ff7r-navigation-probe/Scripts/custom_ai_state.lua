-- Read-only Rebirth custom-AI movement-state observer.
--
-- This probe deliberately does not call any movement, behaviour-tree, route,
-- blackboard-write, or controller-input operation. It samples one moving NPC,
-- its EndAI state/components, generic Unreal path state, and nearby authored
-- route objects. The output is diagnostic only and never authorises movement.

local OUTPUT_PATH = "C:\\CaitGPT\\Telemetry\\navigation-ai-custom-state.json"
local OUTPUT_TEMP_PATH = "C:\\CaitGPT\\Telemetry\\navigation-ai-custom-state.json.tmp"
local SAMPLE_PATH = "C:\\CaitGPT\\Telemetry\\state.sample"
local SAMPLE_PERIOD_MS = 3000
local MAX_ATTEMPTS = 120
local MAX_SAMPLES = 360
local MAX_NPC_SCAN = 96
local TARGET_ACTOR_CLASS = "NP0114_00_BlackMantle_Standard_C"
local MAX_RELEVANT_PROPERTIES = 128
local MAX_ARRAY_ITEMS = 8
local ROUTE_SEARCH_RADIUS_M = 300.0
local UEHelpers = require("UEHelpers")

local function safe(default, callback)
    local ok, value = pcall(callback)
    if ok and value ~= nil then return value end
    return default
end

local function valid(value)
    return safe(false, function() return value ~= nil and value:IsValid() end)
end

local function finite(value)
    return type(value) == "number" and value == value and value > -1.0e30 and value < 1.0e30
end

local function json_escape(value)
    local text = tostring(value or "")
    text = text:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\r", "\\r"):gsub("\n", "\\n")
    return '"' .. text .. '"'
end

local function json_encode(value)
    if value == nil then return "null" end
    local value_type = type(value)
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
        maximum = math.max(maximum, key)
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

local function object_name(value)
    if value == nil then return nil end
    return safe(nil, function() return value:GetFullName() end)
end

local function class_name(value)
    if value == nil then return nil end
    return safe(nil, function() return value:GetClass():GetFullName() end)
end

local function read_sequence()
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then return 0 end
    local line = file:read("*l")
    file:close()
    return tonumber(line and line:match("^(%d+)|")) or 0
end

local function point_table(value)
    if value == nil then return nil end
    local x = safe(nil, function() return value.X end) or safe(nil, function() return value.x end)
    local y = safe(nil, function() return value.Y end) or safe(nil, function() return value.y end)
    local z = safe(nil, function() return value.Z end) or safe(nil, function() return value.z end)
    if not finite(x) or not finite(y) or not finite(z) then return nil end
    return { x = x, y = y, z = z }
end

local function distance_m(first, second)
    if not first or not second then return nil end
    return math.sqrt((second.x - first.x) ^ 2 + (second.y - first.y) ^ 2 + (second.z - first.z) ^ 2) / 100.0
end

local function actor_location(actor)
    return point_table(safe(nil, function() return actor:K2_GetActorLocation() end))
end

local function actor_rotation(actor)
    local rotation = safe(nil, function() return actor:K2_GetActorRotation() end)
    if not rotation then return nil end
    return {
        pitch = safe(nil, function() return rotation.Pitch end),
        yaw = safe(nil, function() return rotation.Yaw end),
        roll = safe(nil, function() return rotation.Roll end),
    }
end

local function vector_from_actor(actor)
    return point_table(safe(nil, function() return actor:GetVelocity() end))
end

local function property_class_name(property)
    return safe(nil, function() return property:GetClass():GetFName():ToString() end) or "unknown"
end

local function raw_value(value, depth)
    if value == nil then return nil end
    if depth and depth > 2 then return tostring(value) end
    local value_type = type(value)
    if value_type == "number" or value_type == "string" or value_type == "boolean" then return value end

    -- Reflected return values may be FName/FVector/other non-UObject userdata.
    -- Calling UObject:GetFullName on those values produces UE4SS overload errors
    -- and can destabilise the game.  Only call object_name/class_name at sites
    -- where the value is known to be a UObject; serialize arbitrary values via
    -- their safe textual/field representations here.
    local text = safe(nil, function() return value:ToString() end)
    if text then return text end

    local fields = {}
    for _, field in ipairs({
        "X", "Y", "Z", "W", "Pitch", "Yaw", "Roll", "R", "G", "B", "A",
        "Name", "EntryName", "KeyName", "Index", "Value", "Location", "Target",
        "Direction", "Velocity", "Translation", "Rotation", "Scale3D",
        "bCanLookAt", "bCanTurn", "bCanWaitAutoTurn", "bCanTurnNotifyCancel",
        "TurnReactionAngle", "ActionDefinition", "MoveDefinition", "ActionDefTag",
        "MoveDefTag", "DefinitionTag", "ReturnTag", "Tag"
    }) do
        local field_value = safe(nil, function() return value[field] end)
        if field_value ~= nil then fields[field] = raw_value(field_value, (depth or 0) + 1) end
    end
    if next(fields) then return fields end
    return tostring(value)
end

local function array_value(array, property, depth)
    local result = {
        count = safe(nil, function() return array:GetArrayNum() end),
        inner_type = safe(nil, function() return property:GetInner():GetClass():GetFName():ToString() end),
        items = {},
    }
    if not result.count or result.count <= 0 then return result end
    pcall(function()
        array:ForEach(function(index, item)
            if #result.items < MAX_ARRAY_ITEMS then
                local resolved = safe(nil, function() return item:get() end) or item
                result.items[#result.items + 1] = { index = index, value = raw_value(resolved, depth or 0) }
            end
        end)
    end)
    return result
end

local function property_value(object, property)
    local name = safe(nil, function() return property:GetFName():ToString() end)
    if not name then return nil end
    local value = safe(nil, function() return object[name] end)
    if safe(false, function() return property:IsA(PropertyTypes.ArrayProperty) end) then
        return array_value(value, property, 0)
    end
    return raw_value(value, 0)
end

local function relevant_property_name(name)
    local lower = string.lower(name or "")
    return lower:find("move") or lower:find("target") or lower:find("destination")
        or lower:find("route") or lower:find("path") or lower:find("patrol")
        or lower:find("waypoint") or lower:find("spline") or lower:find("nav")
        or lower:find("follow") or lower:find("goal") or lower:find("location")
        or lower:find("point") or lower:find("index") or lower:find("state")
        or lower:find("velocity") or lower:find("direction") or lower:find("acceleration")
        or lower:find("movement") or lower:find("action") or lower:find("behavior")
        or lower:find("behaviour") or lower:find("brain") or lower:find("blackboard")
        or lower:find("current") or lower:find("next") or lower:find("node")
        or lower:find("desired") or lower:find("field") or lower:find("ai")
        or lower:find("steer") or lower:find("parkour") or lower:find("push")
        or lower:find("impossible") or lower:find("definition") or lower:find("combo")
        or lower:find("trans") or lower:find("pack") or lower:find("interrupt")
        or lower:find("override")
end

local function inspect_properties(object)
    local result = { identity = object_name(object), class = class_name(object), properties = {} }
    if not valid(object) then return result end
    local class = safe(nil, function() return object:GetClass() end)
    local seen, count = {}, 0
    while valid(class) and count < MAX_RELEVANT_PROPERTIES do
        pcall(function()
            class:ForEachProperty(function(property)
                if count >= MAX_RELEVANT_PROPERTIES then return true end
                local name = safe(nil, function() return property:GetFName():ToString() end)
                if name and not seen[name] and relevant_property_name(name) then
                    seen[name] = true
                    count = count + 1
                    result.properties[name] = {
                        property_type = property_class_name(property),
                        value = property_value(object, property),
                    }
                end
            end)
        end)
        class = safe(nil, function() return class:GetSuperStruct() end)
    end
    result.property_count = count
    return result
end

local function reflection_object(path)
    return safe(nil, function() return StaticFindObject(path) end)
end

local function call_reflected(context, function_object, ...)
    if not valid(context) or not valid(function_object) then return nil, "missing_context_or_function" end
    local arguments = {...}
    local ok, value = pcall(function() return function_object(context, table.unpack(arguments)) end)
    if ok and value ~= nil then return value, nil end
    return nil, ok and "returned_nil" or tostring(value)
end

local function create_reflection()
    local reflection = {
        helper_default = reflection_object("/Script/AIModule.Default__AIBlueprintHelperLibrary"),
        get_ai_controller = reflection_object("/Script/AIModule.AIBlueprintHelperLibrary:GetAIController"),
        current_path = reflection_object("/Script/AIModule.AIBlueprintHelperLibrary:GetCurrentPath"),
        current_path_points = reflection_object("/Script/AIModule.AIBlueprintHelperLibrary:GetCurrentPathPoints"),
        current_path_index = reflection_object("/Script/AIModule.AIBlueprintHelperLibrary:GetCurrentPathIndex"),
        path_destination = reflection_object("/Script/AIModule.PathFollowingComponent:GetPathDestination"),
        path_action_type = reflection_object("/Script/AIModule.PathFollowingComponent:GetPathActionType"),
        get_move_mode = reflection_object("/Script/EndGame.EndAIControllerBluePrintBase:GetAIMoveMode"),
        get_action_state = reflection_object("/Script/EndGame.EndAIControllerEnemyBase:GetAIActionState"),
        get_definition_tag = reflection_object("/Script/EndGame.EndAIControllerEnemyBase:GetCurrentAIDefinitionTag"),
        get_definition_tag_for_chara = reflection_object("/Script/EndGame.EndAIControllerEnemyBase:GetCurrentAIDefinitionTagToChara"),
        get_multi_target_name = reflection_object("/Script/EndGame.EndAIControllerEnemyBase:GetMoveToMultiTargetName"),
        get_next_warp_position = reflection_object("/Script/EndGame.EndAIControllerEnemyBase:GetNextWarpPosition"),
        get_target = reflection_object("/Script/EndGame.EndAIControllerBluePrintBase:GetTarget"),
        get_vector_to_target = reflection_object("/Script/EndGame.EndAIControllerBluePrintBase:GetVectorToTarget"),
    }
    return reflection
end

local function reflected_getters(controller, actor, reflection)
    local result = {}
    local function read(name, function_object, ...)
        local value, error_text = call_reflected(controller, function_object, ...)
        result[name] = { value = raw_value(value, 0), error = error_text }
    end
    read("ai_move_mode", reflection.get_move_mode)
    read("ai_action_state", reflection.get_action_state)
    read("current_ai_definition_tag", reflection.get_definition_tag)
    read("current_ai_definition_tag_to_chara", reflection.get_definition_tag_for_chara, actor)
    read("move_to_multi_target_name", reflection.get_multi_target_name)
    read("next_warp_position", reflection.get_next_warp_position)
    local target, target_error = call_reflected(controller, reflection.get_target)
    result.target_actor = { identity = object_name(target), class = class_name(target), error = target_error }
    result.target_position = actor_location(target)
    local vector_to_target, vector_error = call_reflected(controller, reflection.get_vector_to_target)
    result.vector_to_target = { value = point_table(vector_to_target) or raw_value(vector_to_target, 0), error = vector_error }
    return result
end

local function generic_path_state(actor, controller, reflection)
    local result = {}
    local path, path_error = call_reflected(reflection.helper_default, reflection.current_path, controller)
    local points, points_error = call_reflected(reflection.helper_default, reflection.current_path_points, controller)
    local index, index_error = call_reflected(reflection.helper_default, reflection.current_path_index, controller)
    local path_component = safe(nil, function() return controller.PathFollowingComponent end)
    local destination, destination_error = call_reflected(path_component, reflection.path_destination)
    local action_type, action_error = call_reflected(path_component, reflection.path_action_type)
    local point_list = {}
    if type(points) == "table" then
        for _, point in ipairs(points) do point_list[#point_list + 1] = point_table(point) end
    else
        pcall(function() points:ForEach(function(_, point) point_list[#point_list + 1] = point_table(point) end) end)
    end
    result.path = { identity = object_name(path), error = path_error }
    result.path_points = { count = #point_list, points = point_list, error = points_error }
    result.path_index = { value = raw_value(index, 0), error = index_error }
    result.path_destination = { value = point_table(destination), error = destination_error }
    result.path_action_type = { value = raw_value(action_type, 0), error = action_error }
    result.path_following_component = object_name(path_component)
    return result
end

local function blackboard_state(blackboard)
    if not valid(blackboard) then return { available = false } end
    local result = { available = true, identity = object_name(blackboard), class = class_name(blackboard) }
    local asset = safe(nil, function() return blackboard.BlackboardAsset end)
    result.asset = object_name(asset)
    result.asset_state = inspect_properties(asset)
    local key_array = safe(nil, function() return asset.Keys end)
    result.keys = {}
    if key_array then
        pcall(function()
            key_array:ForEach(function(_, entry)
                if #result.keys >= 32 then return end
                local name = safe(nil, function() return entry.EntryName:ToString() end)
                    or safe(nil, function() return entry.KeyName:ToString() end)
                if name then
                    local fname = safe(nil, function() return UEHelpers.FindOrAddFName(name) end)
                    local values = {}
                    for _, getter in ipairs({ "GetValueAsObject", "GetValueAsVector", "GetValueAsName", "GetValueAsInt", "GetValueAsFloat", "GetValueAsBool" }) do
                        local ok, value = pcall(function() return blackboard[getter](blackboard, fname) end)
                        if ok then values[getter] = raw_value(value, 0) end
                    end
                    result.keys[#result.keys + 1] = { name = name, values = values }
                end
            end)
        end)
    end
    return result
end

local function spline_component_state(component)
    if not valid(component) then return nil end
    local result = inspect_properties(component)
    result.spline_point_count = safe(nil, function() return component:GetNumberOfSplinePoints() end)
    result.points = {}
    if result.spline_point_count then
        for index = 0, math.min(result.spline_point_count - 1, 31) do
            local point = safe(nil, function() return component:GetLocationAtSplinePoint(index, 1) end)
            result.points[#result.points + 1] = { index = index, location = point_table(point) }
        end
    end
    return result
end

local function authored_route_state(actor_position)
    local result, seen = {}, {}
    for _, class in ipairs({ "EndMobRouteSpline", "EndMobRouteSplineNonLoop", "EndMovePoint", "EndPathWaySplineActor", "EndSplineActorBase", "EndAIInterestPoint" }) do
        local objects = safe(nil, function() return FindAllOf(class) end)
        if type(objects) == "table" then
            for _, object in pairs(objects) do
                local identity, position = object_name(object), actor_location(object)
                if identity and position and not seen[identity] and distance_m(actor_position, position) <= ROUTE_SEARCH_RADIUS_M then
                    seen[identity] = true
                    local route = inspect_properties(object)
                    route.position = position
                    route.distance_from_tracked_actor_m = distance_m(actor_position, position)
                    local component = safe(nil, function() return object.SplineMoveComponent end)
                        or safe(nil, function() return object.MobRouteSplineComponent end)
                        or safe(nil, function() return object.PathWayComponent end)
                        or safe(nil, function() return object.SplineComponent end)
                    route.spline_component = spline_component_state(component)
                    result[#result + 1] = route
                    if #result >= 64 then return result end
                end
            end
        end
    end
    return result
end

local function find_controller(actor, reflection)
    local controller = select(1, call_reflected(reflection.helper_default, reflection.get_ai_controller, actor))
    if valid(controller) then return controller end
    return safe(nil, function() return actor.Controller end)
end

local function related_state(actor, controller)
    local move_component = safe(nil, function() return controller.AIMoveComponent end)
    local action_component = safe(nil, function() return controller.AIActionComponent end)
    local brain_component = safe(nil, function() return controller.BrainComponent end)
    local blackboard = safe(nil, function() return controller.Blackboard end)
    if not valid(blackboard) then blackboard = safe(nil, function() return controller:GetBlackboardComponent() end) end
    local movement = safe(nil, function() return actor.CharacterMovement end)
    return {
        controller = inspect_properties(controller),
        ai_move_component = inspect_properties(move_component),
        ai_action_component = inspect_properties(action_component),
        brain_component = inspect_properties(brain_component),
        blackboard_component = blackboard_state(blackboard),
        character_movement_component = inspect_properties(movement),
    }
end

local function changed_fields(previous, current)
    local changed = {}
    local function compare(prefix, old_value, new_value)
        local old_json, new_json = json_encode(old_value), json_encode(new_value)
        if old_json ~= new_json then changed[prefix] = { before = old_value, after = new_value } end
    end
    if not previous then return changed end
    for object_key, object_state in pairs(current.related_objects or {}) do
        local old_object = previous.related_objects and previous.related_objects[object_key]
        for name, value in pairs(object_state.properties or {}) do
            compare(object_key .. "." .. name, old_object and old_object.properties and old_object.properties[name], value)
        end
    end
    for name, value in pairs(current.controller_getters or {}) do
        compare("controller_getters." .. name, previous.controller_getters and previous.controller_getters[name], value)
    end
    return changed
end

local function write_observation(record)
    local file = io.open(OUTPUT_TEMP_PATH, "wb")
    if not file then return end
    file:write(json_encode(record) .. "\n")
    file:flush(); file:close()
    os.remove(OUTPUT_PATH); os.rename(OUTPUT_TEMP_PATH, OUTPUT_PATH)
end

local attempts, samples = 0, {}
local finished, tracked_actor, tracked_id, previous_state = false, nil, nil, nil
local reflection = nil
local previous_positions = {}
local route_cache = nil

local function choose_actor()
    if valid(tracked_actor) then return tracked_actor end
    -- Black Mantle is the known patrolling class in the town-square evidence.
    -- Resolve only that exact class: broad EndCharacter enumeration caused
    -- repeated live crashes before any diagnostic sample was produced.
    local objects = safe(nil, function() return FindAllOf(TARGET_ACTOR_CLASS) end)
    -- Require one completed displacement sample before selecting the actor.
    -- This avoids locking onto a stationary Black Mantle instance.
    local best, best_displacement = nil, 0.05
    if type(objects) ~= "table" then return nil end
    local inspected = 0
    for _, actor in pairs(objects) do
        inspected = inspected + 1
        if inspected > MAX_NPC_SCAN then break end
        if valid(actor) then
            local identity, position = object_name(actor), actor_location(actor)
            if identity and position then
                local displacement = previous_positions[identity] and distance_m(previous_positions[identity], position) or 0.0
                previous_positions[identity] = position
                if displacement >= best_displacement then best, best_displacement = actor, displacement end
            end
        end
    end
    return best
end

local function run_observer()
    if finished then return end
    attempts = attempts + 1
    if not reflection then reflection = create_reflection() end
    local record = {
        schema_version = 1,
        source = "ue4ss_rebirth_custom_ai_state_observation",
        timestamp_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        sample_period_ms = SAMPLE_PERIOD_MS,
        attempt = attempts,
        max_attempts = MAX_ATTEMPTS,
        map_id = "EndGame",
        movement_sent = false,
        ai_movement_request_sent = false,
        reflection = {},
        samples = samples,
        status = "sampling",
        reason_code = "awaiting_moving_npc",
        observed_class_paths = {
            "/Script/EndGame.EndAIMoveComponent",
            "/Script/EndGame.EndAIActionComponent",
            "/Script/EndGame.EndAIBehaviorTreeComponent",
            "/Script/EndGame.EndMobRouteSpline",
            "/Script/EndGame.EndMobRouteSplineNonLoop",
            "/Script/EndGame.EndMovePoint",
            "/Script/EndGame.EndPathWaySplineActor",
            "/Script/EndGame.EndAIInterestPoint",
        },
    }
    for name, value in pairs(reflection) do record.reflection[name] = valid(value) end

    local actor = choose_actor()
    if not valid(actor) then
        record.status = attempts >= MAX_ATTEMPTS and "inconclusive" or "waiting_for_npc"
        record.reason_code = attempts >= MAX_ATTEMPTS and "no_trackable_npc" or "no_trackable_npc_yet"
        if attempts >= MAX_ATTEMPTS then finished = true end
        write_observation(record)
        return
    end
    tracked_actor = actor
    tracked_id = object_name(actor)
    local controller = find_controller(actor, reflection)
    local position = actor_location(actor)
    local rotation = actor_rotation(actor)
    local velocity = vector_from_actor(actor)
    local previous_position = previous_state and previous_state.actor_position or nil
    local displacement = distance_m(previous_position, position)
    local movement_bearing = displacement and previous_position and math.deg(math.atan(position.y - previous_position.y, position.x - previous_position.x)) or nil
    local yaw_delta = nil
    if previous_state and previous_state.facing_yaw and rotation and rotation.yaw then
        yaw_delta = (rotation.yaw - previous_state.facing_yaw + 540.0) % 360.0 - 180.0
    end
    local sample = {
        sample_index = #samples + 1,
        timestamp_utc = record.timestamp_utc,
        base_sequence = read_sequence(),
        actor = tracked_id,
        actor_class = class_name(actor),
        controller = object_name(controller),
        controller_class = class_name(controller),
        actor_position = position,
        velocity = velocity,
        facing_yaw = rotation and rotation.yaw or nil,
        displacement_since_previous_sample_m = displacement,
        movement_bearing_degrees = movement_bearing,
        yaw_delta_degrees = yaw_delta,
        controller_getters = valid(controller) and reflected_getters(controller, actor, reflection) or {},
        related_objects = valid(controller) and related_state(actor, controller) or {},
        generic_unreal_path = valid(controller) and generic_path_state(actor, controller, reflection) or {},
    }
    if tracked_id ~= (previous_state and previous_state.actor or nil) then
        route_cache = position and authored_route_state(position) or {}
        sample.authored_route_objects = route_cache
        sample.track_transition = true
    elseif #samples == 0 then
        route_cache = position and authored_route_state(position) or {}
        sample.authored_route_objects = route_cache
    else
        sample.authored_route_object_count = #route_cache
    end
    sample.changed_fields = changed_fields(previous_state, sample)
    samples[#samples + 1] = sample
    if #samples > MAX_SAMPLES then table.remove(samples, 1) end
    previous_state = sample
    record.status = "sampling"
    record.reason_code = "tracking_npc"
    record.tracked_actor = tracked_id
    record.sample_count = #samples
    if attempts >= MAX_ATTEMPTS then
        finished = true
        record.status = "completed"
        record.reason_code = "bounded_observation_completed"
    end
    write_observation(record)
end

LoopAsync(SAMPLE_PERIOD_MS, function()
    ExecuteInGameThread(function() pcall(run_observer) end)
    return false
end)
