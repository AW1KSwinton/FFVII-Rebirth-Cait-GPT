-- Read-only bounded navigation-clearance probe.
-- Publishes four camera-relative corridors at three heights. It is local
-- collision evidence, not a global route planner.
local SAMPLE_PATH = "C:\\CaitGPT\\Telemetry\\state.sample"
local OUTPUT_PATH = "C:\\CaitGPT\\Telemetry\\navigation.json"
local TEMP_PATH = "C:\\CaitGPT\\Telemetry\\navigation.json.tmp"
local HEALTH_PATH = "C:\\CaitGPT\\Telemetry\\navigation-health.json"
local HEALTH_TEMP_PATH = "C:\\CaitGPT\\Telemetry\\navigation-health.json.tmp"
local UEHelpers = require("object_refs")
-- Keep the cheap line-trace cadence below the historical 250 ms stress point
-- on the lower-end development laptop. The local controller already gates
-- movement on fresh, sequence-bound evidence, so 500 ms remains responsive
-- enough for bounded navigation without an unnecessary 4 Hz game-thread load.
local SAMPLE_PERIOD_MS = 500
local TRACE_DISTANCE_CM = 1200.0
local TRACE_Z_OFFSETS_CM = { 40.0, 100.0, 160.0 }
local TRACE_CHANNEL = 0
-- A surface whose normal points mostly upward may be supporting terrain. This
-- is evidence only: a capsule sweep is still required before movement can
-- trust a corridor as character-walkable.
local WALKABLE_NORMAL_Z_MIN = 0.82
local WALKABLE_SLOPE_MAX_DEGREES = 35.0
-- Capsule sweeps are deliberately much less frequent than the cheap line
-- traces. Four character-sized sweeps every four probe samples is roughly
-- one sweep set per two seconds in the live exporter. The long sweep gives
-- the coordinator a usable horizon for one bounded long hold; the line-trace
-- cadence remains deliberately below the historical 250 ms stress point.
-- Spread the four expensive character-sized sweeps across successive samples:
-- one sweep per 500 ms callback, rotating through the four headings. This
-- preserves a fresh result for each heading every two seconds without issuing
-- a four-sweep game-thread burst that destabilised Rebirth.
local CAPSULE_PROBE_DIRECTIONS = {"forward", "backward", "left", "right"}
local capsule_direction_index = 0
local CAPSULE_PROBE_INTERVAL_SECONDS = 2
local CAPSULE_TRACE_DISTANCE_CM = 4000.0
local CAPSULE_RADIUS_CM = 42.0
local CAPSULE_HALF_HEIGHT_CM = 96.0
-- Actor locations are at the character capsule centre. Lift the swept volume
-- to its centre-height corridor so the supporting floor and low terrain do
-- not win the single-hit sweep before a wall or fence can be observed.
local CAPSULE_START_LIFT_CM = CAPSULE_HALF_HEIGHT_CM
local GetKismetSystemLibrary = UEHelpers.GetKismetSystemLibrary
local GetKismetMathLibrary = UEHelpers.GetKismetMathLibrary
local GetPlayerController = UEHelpers.GetPlayerController

local producer_started_utc = os.date("!%Y-%m-%dT%H:%M:%SZ")
local callback_attempts = 0
local capture_successes = 0
local capture_failures = 0
local schedule_failures = 0
local last_attempt_utc = ""
local last_success_utc = ""
local last_success_sequence = nil
local producer_state = "starting"
local last_failure_reason = ""
local last_failure_detail = ""
local health_ticks = 0
local last_health_state = ""
local idle_reason = ""

local function safe(default, callback)
    local ok, value = pcall(callback)
    if not ok or value == nil then return default end
    return value
end
local function valid(value)
    return safe(false, function() return value ~= nil and value:IsValid() end)
end
local function finite(value)
    return type(value) == "number" and value == value and value > -1.0e30 and value < 1.0e30
end
local function json_number(value)
    return finite(value) and string.format("%.6f", value) or "null"
end
local function json_escape(value)
    local text = tostring(value or "")
    text = text:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\r", "\\r"):gsub("\n", "\\n")
    return '"' .. text .. '"'
end

local function record_capture_failure(reason, detail)
    capture_failures = capture_failures + 1
    producer_state = "degraded"
    last_failure_reason = tostring(reason or "capture_failed")
    last_failure_detail = tostring(detail or "")
end

local function record_schedule_failure(detail)
    schedule_failures = schedule_failures + 1
    producer_state = "degraded"
    last_failure_reason = "execute_in_game_thread_failed"
    last_failure_detail = tostring(detail or "")
end

local function write_health()
    local payload = string.format(
        '{"schema_version":1,"producer":"FF7RNavigationProbe","state":%s,"started_at_utc":%s,"callback_attempts":%d,"capture_successes":%d,"capture_failures":%d,"schedule_failures":%d,"last_attempt_utc":%s,"last_success_utc":%s,"last_success_sequence":%s,"last_failure_reason":%s,"last_failure_detail":%s,"idle_reason":%s}\n',
        json_escape(producer_state),
        json_escape(producer_started_utc),
        callback_attempts,
        capture_successes,
        capture_failures,
        schedule_failures,
        json_escape(last_attempt_utc),
        json_escape(last_success_utc),
        last_success_sequence and tostring(last_success_sequence) or "null",
        json_escape(last_failure_reason),
        json_escape(last_failure_detail),
        json_escape(idle_reason)
    )
    local file = io.open(HEALTH_TEMP_PATH, "wb")
    if not file then return false end
    local ok = pcall(function()
        file:write(payload)
        file:flush()
        file:close()
        os.remove(HEALTH_PATH)
        assert(os.rename(HEALTH_TEMP_PATH, HEALTH_PATH))
    end)
    if not ok then
        pcall(function() file:close() end)
        os.remove(HEALTH_TEMP_PATH)
    end
    return ok
end

local function maybe_write_health(force)
    health_ticks = health_ticks + 1
    if force or producer_state ~= last_health_state or health_ticks % 4 == 0 then
        if write_health() then
            last_health_state = producer_state
        end
    end
end
local function clamp(value, minimum, maximum)
    return math.max(minimum, math.min(maximum, value))
end
local function slope_degrees(normal_z)
    if not finite(normal_z) then return nil end
    return math.deg(math.acos(clamp(normal_z, -1.0, 1.0)))
end
local function read_field_sample()
    local file = io.open(SAMPLE_PATH, "rb")
    if not file then return nil, "state_sample_unavailable" end
    local line = file:read(16385)
    file:close()
    if not line or #line > 16384 then return nil, "state_sample_unavailable" end
    line = line:match("^[^\r\n]+")
    local fields = {}
    for value in ((line or "") .. "|"):gmatch("(.-)|") do
        fields[#fields + 1] = value
        if #fields == 17 then break end
    end
    local sequence = tonumber(fields[1])
    local stamp = fields[2] or ""
    if not sequence or sequence < 1 or sequence % 1 ~= 0 or #fields < 15
        or not stamp:match("^%d%d%d%d%-%d%d%-%d%dT%d%d:%d%d:%d%dZ$") then
        return nil, "state_sample_unavailable"
    end
    -- The existing producer writes UTC seconds. Read its cheap primitive
    -- boundary before resolving a controller or invoking any trace functions.
    if stamp < os.date("!%Y-%m-%dT%H:%M:%SZ", os.time() - 2)
        or stamp > os.date("!%Y-%m-%dT%H:%M:%SZ", os.time() + 1) then
        return nil, "state_sample_stale"
    end
    if fields[10] ~= "0" or fields[12] ~= "0" then return nil, "paused_or_menu" end
    if fields[6] == "" or fields[7] == "" or fields[15] == "" then
        return nil, "player_not_loaded"
    end
    return sequence
end
local function actor_location(actor)
    return safe(nil, function() return actor:K2_GetActorLocation() end)
end
-- Vector arithmetic in Lua: each KismetMathLibrary call is a marshalled
-- UFunction call on the game thread, about three per trace.
local function vector_add(a, b)
    return {X = a.X + b.X, Y = a.Y + b.Y, Z = a.Z + b.Z}
end
local function vector_scale(vector, distance)
    return {X = vector.X * distance, Y = vector.Y * distance, Z = vector.Z * distance}
end
local function vector_distance(a, b)
    return safe(nil, function()
        local dx, dy, dz = a.X - b.X, a.Y - b.Y, a.Z - b.Z
        return math.sqrt(dx * dx + dy * dy + dz * dz)
    end)
end
local function trace_once(world, pawn, start_vector, direction)
    local end_vector = vector_add(start_vector, vector_scale(direction, TRACE_DISTANCE_CM))
    local hit = {}
    local was_hit = safe(false, function()
        return GetKismetSystemLibrary():LineTraceSingle(world, start_vector, end_vector, TRACE_CHANNEL, false, { pawn }, 0, hit, true, {R=0,G=255,B=0,A=255}, {R=255,G=0,B=0,A=255}, 0.0)
    end)
    if not was_hit then return TRACE_DISTANCE_CM / 100.0, false, nil, nil end
    -- For a line trace, FHitResult.Distance is the travel distance along the
    -- ray. Keep the same conservative fallback for bindings that omit it.
    local distance = safe(nil, function() return hit.Distance end)
    if not finite(distance) or distance < 0 then
        local impact = safe(nil, function() return hit.ImpactPoint end)
        distance = impact and vector_distance(start_vector, impact) or nil
    end
    local normal = safe(nil, function() return hit.ImpactNormal or hit.Normal end)
    local normal_z = safe(nil, function() return normal.Z end)
    local slope = slope_degrees(normal_z)
    if not finite(distance) or distance < 0 then return TRACE_DISTANCE_CM / 100.0, true, normal_z, slope end
    return math.min(TRACE_DISTANCE_CM, distance) / 100.0, true, normal_z, slope
end
local function trace_corridor(world, pawn, start_vector, direction)
    local minimum, any_hit = TRACE_DISTANCE_CM / 100.0, false
    local blocking_hit, walkable_surface_hit = false, false
    local nearest_normal_z, nearest_slope = nil, nil
    for _, offset in ipairs(TRACE_Z_OFFSETS_CM) do
        local distance, hit, normal_z, slope = trace_once(world, pawn, vector_add(start_vector, {X=0.0,Y=0.0,Z=offset}), direction)
        minimum, any_hit = math.min(minimum, distance), any_hit or hit
        if hit then
            if normal_z ~= nil and slope ~= nil
                and normal_z >= WALKABLE_NORMAL_Z_MIN
                and slope <= WALKABLE_SLOPE_MAX_DEGREES then
                walkable_surface_hit = true
            else
                -- Missing normal data remains blocking evidence. The probe
                -- must never turn unknown collision into permission to move.
                blocking_hit = true
            end
            if distance <= minimum then
                nearest_normal_z, nearest_slope = normal_z, slope
            end
        end
    end
    local surface_class = "clear"
    if any_hit then
        if blocking_hit then
            surface_class = "blocking_or_unknown"
        elseif walkable_surface_hit then
            surface_class = "walkable_surface_candidate"
        else
            surface_class = "unknown_hit"
        end
    end
    return minimum, any_hit, blocking_hit, walkable_surface_hit, nearest_normal_z, nearest_slope, surface_class
end
local function capsule_trace_once(world, pawn, start_vector, direction)
    local lifted_start = vector_add(start_vector, {X=0.0,Y=0.0,Z=CAPSULE_START_LIFT_CM})
    local end_vector = vector_add(lifted_start, vector_scale(direction, CAPSULE_TRACE_DISTANCE_CM))
    local hit = {}
    local ok, was_hit = pcall(function()
        return GetKismetSystemLibrary():CapsuleTraceSingle(
            world,
            lifted_start,
            end_vector,
            CAPSULE_RADIUS_CM,
            CAPSULE_HALF_HEIGHT_CM,
            TRACE_CHANNEL,
            false,
            { pawn },
            0,
            hit,
            true,
            {R=0,G=128,B=255,A=255},
            {R=255,G=128,B=0,A=255},
            0.0
        )
    end)
    if not ok then
        return false, false, nil, nil, "capsule_trace_unavailable"
    end
    if not was_hit then
        return true, false, CAPSULE_TRACE_DISTANCE_CM / 100.0, nil, "clear"
    end
    -- For a capsule sweep, FHitResult.Distance is the travel distance of the
    -- swept shape. ImpactPoint is on the contacted surface and would measure
    -- from the capsule centre to the floor instead of along the sweep path.
    local distance = safe(nil, function() return hit.Distance end)
    if not finite(distance) or distance < 0 then
        local impact = safe(nil, function() return hit.ImpactPoint end)
        distance = impact and vector_distance(lifted_start, impact) or nil
    end
    local normal = safe(nil, function() return hit.ImpactNormal or hit.Normal end)
    local normal_z = safe(nil, function() return normal.Z end)
    local slope = slope_degrees(normal_z)
    local blocking = not (
        normal_z ~= nil
        and slope ~= nil
        and normal_z >= WALKABLE_NORMAL_Z_MIN
        and slope <= WALKABLE_SLOPE_MAX_DEGREES
    )
    local surface_class = blocking and "blocking_or_unknown" or "walkable_surface_candidate"
    if not finite(distance) or distance < 0 then distance = CAPSULE_TRACE_DISTANCE_CM / 100.0 end
    return true, true, math.min(CAPSULE_TRACE_DISTANCE_CM, distance) / 100.0, normal_z, surface_class, slope
end
local function capsule_probe_payload(name, world, pawn, start_vector, direction, sequence, run_capsule_probe)
    -- Only the selected heading is swept on this callback. The other headings
    -- remain explicitly stale until their own round-robin sample arrives.
    if not run_capsule_probe then
        return '{"available":false,"fresh":false,"reason":"scheduled_interval","sample_sequence":' .. tostring(sequence) .. '}'
    end
    local available, hit, distance, normal_z, surface_class, slope = capsule_trace_once(world, pawn, start_vector, direction)
    if not available then
        return '{"available":false,"fresh":true,"reason":"capsule_trace_unavailable","sample_sequence":' .. tostring(sequence) .. '}'
    end
    local blocking = hit and surface_class == "blocking_or_unknown"
    return string.format('{"available":true,"fresh":true,"hit":%s,"blocking_hit":%s,"walkable_surface_hit":%s,"corridor_confirmed":%s,"clear_distance_m":%s,"hit_normal_z":%s,"hit_slope_degrees":%s,"surface_class":%s,"radius_m":%s,"half_height_m":%s,"start_lift_m":%s,"sample_sequence":%d}', hit and "true" or "false", blocking and "true" or "false", (hit and not blocking) and "true" or "false", (not hit) and "true" or "false", json_number(distance), json_number(normal_z), json_number(slope), json_escape(surface_class), json_number(CAPSULE_RADIUS_CM / 100.0), json_number(CAPSULE_HALF_HEIGHT_CM / 100.0), json_number(CAPSULE_START_LIFT_CM / 100.0), sequence)
end
local function direction_payload(name, direction, start_vector, world, pawn, bearing, sequence, run_capsule_probe)
    local clear_distance, hit, blocking_hit, walkable_surface_hit, normal_z, slope, surface_class = trace_corridor(world, pawn, start_vector, direction)
    local capsule = capsule_probe_payload(name, world, pawn, start_vector, direction, sequence, run_capsule_probe)
    return string.format('"%s":{"clear_distance_m":%s,"progress_m":%s,"hit":%s,"blocking_hit":%s,"walkable_surface_hit":%s,"hit_normal_z":%s,"hit_slope_degrees":%s,"surface_class":%s,"capsule_probe":%s,"bearing_degrees":%s,"confidence":"line_trace_multi_height_surface_normal","probe_kind":"line_trace_surface_normal","sample_sequence":%d}', name, json_number(clear_distance), json_number(clear_distance), hit and "true" or "false", blocking_hit and "true" or "false", walkable_surface_hit and "true" or "false", json_number(normal_z), json_number(slope), json_escape(surface_class), capsule, json_number(bearing), sequence)
end
local function clear_output()
    os.remove(OUTPUT_PATH); os.remove(TEMP_PATH)
end
local function write_payload(payload)
    local file = io.open(TEMP_PATH, "wb")
    if not file then return false, "navigation_output_open_failed" end
    local ok, detail = pcall(function()
        assert(file:write(payload))
        assert(file:flush())
        assert(file:close())
        os.remove(OUTPUT_PATH)
        assert(os.rename(TEMP_PATH, OUTPUT_PATH))
    end)
    if not ok then
        pcall(function() file:close() end)
        os.remove(TEMP_PATH)
        return false, tostring(detail or "navigation_output_write_failed")
    end
    return true
end
local function write_probe()
    local sequence, boundary_reason = read_field_sample()
    if not sequence then
        clear_output()
        producer_state = "idle"
        idle_reason = boundary_reason
        return false
    end
    idle_reason = ""
    capsule_direction_index = (capsule_direction_index % #CAPSULE_PROBE_DIRECTIONS) + 1
    local capsule_direction = CAPSULE_PROBE_DIRECTIONS[capsule_direction_index]
    local controller = safe(nil, function() return GetPlayerController() end)
    local pawn = valid(controller) and safe(nil, function() return controller.Pawn end) or nil
    local camera = valid(controller) and safe(nil, function() return controller.PlayerCameraManager end) or nil
    if not valid(pawn) or not valid(camera) then
        clear_output()
        record_capture_failure("gameplay_objects_unavailable")
        return false, "gameplay_objects_unavailable"
    end
    local system, math_library = safe(nil, GetKismetSystemLibrary), safe(nil, GetKismetMathLibrary)
    if not valid(system) or not valid(math_library) then
        clear_output()
        record_capture_failure("kismet_api_unavailable")
        return false, "kismet_api_unavailable"
    end
    local start = actor_location(pawn)
    local rotation = safe(nil, function() return camera:GetCameraRotation() end)
    if not start or not rotation then
        clear_output()
        record_capture_failure("player_pose_unavailable")
        return false, "player_pose_unavailable"
    end
    -- Movement and corridor checks are horizontal. The normal downward
    -- gameplay pitch would otherwise make forward rays hit the ground and
    -- falsely turn clear paths into walls.
    local horizontal_rotation = {Pitch=0.0, Yaw=rotation.Yaw, Roll=0.0}
    local forward = safe(nil, function() return math_library:GetForwardVector(horizontal_rotation) end)
    local right = safe(nil, function() return math_library:GetRightVector(horizontal_rotation) end)
    if not forward or not right then
        clear_output()
        record_capture_failure("direction_vectors_unavailable")
        return false, "direction_vectors_unavailable"
    end
    local yaw = safe(0.0, function() return rotation.Yaw end)
    local directions = table.concat({ direction_payload("forward", forward, start, pawn, pawn, yaw, sequence, capsule_direction == "forward"), direction_payload("backward", vector_scale(forward, -1.0), start, pawn, pawn, yaw + 180.0, sequence, capsule_direction == "backward"), direction_payload("left", vector_scale(right, -1.0), start, pawn, pawn, yaw - 90.0, sequence, capsule_direction == "left"), direction_payload("right", right, start, pawn, pawn, yaw + 90.0, sequence, capsule_direction == "right") }, ",")
    local ok, detail = write_payload(string.format('{"schema_version":1,"base_sequence":%d,"timestamp_utc":%s,"source":"ue4ss_line_trace_multi_height_capsule","probe_kind":"line_trace_surface_normal_capsule","sample_period_ms":%d,"capsule_probe_interval_seconds":%d,"capsule_probe_cycle":true,"capsule_probe_direction":%s,"capsule_trace_distance_m":%s,"capsule_radius_m":%s,"capsule_half_height_m":%s,"capsule_start_lift_m":%s,"max_trace_distance_m":%s,"directions":{%s}}\n', sequence, json_escape(os.date("!%Y-%m-%dT%H:%M:%SZ")), SAMPLE_PERIOD_MS, CAPSULE_PROBE_INTERVAL_SECONDS, json_escape(capsule_direction), json_number(CAPSULE_TRACE_DISTANCE_CM / 100.0), json_number(CAPSULE_RADIUS_CM / 100.0), json_number(CAPSULE_HALF_HEIGHT_CM / 100.0), json_number(CAPSULE_START_LIFT_CM / 100.0), json_number(TRACE_DISTANCE_CM / 100.0), directions))
    if not ok then
        record_capture_failure("navigation_output_write_failed", detail)
        return false, detail
    end
    capture_successes = capture_successes + 1
    producer_state = "running"
    last_success_utc = os.date("!%Y-%m-%dT%H:%M:%SZ")
    last_success_sequence = sequence
    return true
end
-- Called only by the paired native persistent game-thread scheduler.
function FF7RReadOnlyTick()
    callback_attempts = callback_attempts + 1
    last_attempt_utc = os.date("!%Y-%m-%dT%H:%M:%SZ")
    local callback_ok, probe_ok, probe_detail = pcall(write_probe)
    if not callback_ok then
        record_capture_failure("write_probe_exception", probe_ok)
    elseif probe_ok ~= true and probe_detail then
        last_failure_detail = tostring(probe_detail)
    end
    pcall(function() maybe_write_health(false) end)
end
