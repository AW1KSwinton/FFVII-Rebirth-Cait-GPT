local SAMPLE_PATH = "C:\\CaitGPT\\Telemetry\\state.sample"
local HEALTH_PATH = "C:\\CaitGPT\\Telemetry\\telemetry-health.json"
local HEALTH_TEMP_PATH = "C:\\CaitGPT\\Telemetry\\telemetry-health.json.tmp"
local UEHelpers = require("object_refs")
local entity_poi_ok, entity_poi = pcall(require, "entity_poi")
local map_query_ok, map_query = pcall(require, "map_query")
local route_sweep_ok, route_sweep = pcall(require, "route_sweep")
local cutscene_ok, cutscene = pcall(require, "cutscene")
local objective_query_ok, objective_query = pcall(require, "objective_query")
local combat_query_ok, combat_query = pcall(require, "combat_query")
local combat_query_active = false
local combat_pawn_source = "none"
local sequence = 0
local reported_sample_error = false
local reported_sample_schedule_error = false
local reported_menu_cache_error = false
local reported_menu_schedule_error = false
local reported_bench_cache_error = false
local reported_bench_schedule_error = false
local cached_in_menu = -1
-- FindAllOf walks the whole object array on the game thread; run every ten
-- samples it cost a 0.5-0.8 s gap each time (live 2026-09-27, one in ten
-- sequences), a visible hitch. The menu widget outlives the menu, so the
-- instance paths are kept and freshly resolved before visibility reads; the full scan
-- repeats rarely, to catch a widget created later.
local menu_instances = {}
local MENU_RESCAN_TICKS = 300
local MENU_RESCAN_EMPTY_TICKS = 50
local menu_ticks_since_scan = math.huge
-- Loading a save rebuilds the menu widget while the old, hidden instance
-- stays valid, so the cached list never showed the new one until the next
-- full rescan (live: menu open, paused, in_menu false). Rescan once when the
-- game becomes paused with no cached menu visible (the menu opening). Not
-- repeatedly while paused: that also covers a save load tearing objects down,
-- and the game crashed during a load with a 2 s paused rescan deployed.
local last_sample_paused = -1
local menu_pause_rescanned = false
-- Wall time of the periodic caches, reported in the health file.
local periodic_ms = {menu_last = 0, menu_max = 0, bench_last = 0, bench_max = 0}
-- Cumulative wall time per part of the tick, so a reader can difference two
-- health files and see which part costs game-thread time.
local section_ms = {sample = 0, poi = 0, map_query = 0, route_sweep = 0, write = 0, menu = 0, bench = 0,
    party = 0, pose = 0, world = 0, battle = 0, locomotion = 0, cutscene = 0, objective_query = 0, combat_query = 0}
-- Timing is diagnostics only; a missing clock must not stop sampling.
local clock = (os and os.clock) or function() return 0 end
local function timed_section(name, started)
    section_ms[name] = section_ms[name] + (clock() - started) * 1000
end
local cached_bench = ""
local BENCH_DETECTION_RADIUS_CM = 500.0
local BENCH_USABLE_RADIUS_CM = 300.0
local MAX_COMMAND_WIDGETS = 8
local MAX_COMMAND_TREE_WIDGETS = 64
local MAX_COMMAND_TREE_DEPTH = 8
local locomotion_diagnostic = "not_sampled"
-- What the battle pawn fallback last saw, for controlled live acceptance.
local combat_pawn_diagnostic = "not_sampled"
local producer_started_utc = os.date("!%Y-%m-%dT%H:%M:%SZ")
local callback_attempts = 0
local capture_successes = 0
local capture_failures = 0
local schedule_failures = 0
local consecutive_failures = 0
local last_attempt_utc = ""
local last_success_utc = ""
local last_success_sequence = nil
local last_failure_reason = ""
local last_failure_detail = ""
local producer_state = "starting"
local health_ticks = 0
local last_health_state = ""
local producer_tick = 0
local sample_generation = 0

local function safe_call(default, callback)
    local ok, value = pcall(callback)
    if not ok or value == nil then
        return default
    end
    return value
end

local function find_object(path)
    return UEHelpers.resolve(path)
end

local function get_api(path)
    local found, object = pcall(find_object, path)
    if not found or object == nil then
        return nil
    end
    local valid, is_valid = pcall(function() return object:IsValid() end)
    if valid and is_valid then
        return object
    end
    return nil
end

local function clean_text(value)
    if value == nil then
        return ""
    end

    local converted = safe_call(nil, function()
        return value:ToString()
    end)
    if converted ~= nil then
        value = converted
    end

    return tostring(value):gsub("[|,\r\n]", "_")
end

local function object_valid(value)
    return safe_call(false, function() return value ~= nil and value:IsValid() end)
end

-- Live 2026-10-20 (Mt. Nibel, Classic mode): in battle the player
-- controller is valid but its Pawn is not, so the field reads lose the map,
-- the actor and IsInBattle. The camera keeps viewing the controlled party
-- character: PlayerCameraManager.ViewTarget.Target (reflected members only;
-- a failed member lookup is remembered so it is never retried per tick).
local view_target_unreadable = false
local function battle_view_pawn(controller)
    if view_target_unreadable then combat_pawn_diagnostic = "view_target_unreadable"; return nil end
    if not object_valid(controller) then combat_pawn_diagnostic = "controller_invalid"; return nil end
    local ok, target = pcall(function()
        local camera = controller.PlayerCameraManager
        if camera == nil or not camera:IsValid() then return false end
        return camera.ViewTarget.Target
    end)
    if not ok then view_target_unreadable = true; combat_pawn_diagnostic = "view_target_unreadable"; return nil end
    if target == false then combat_pawn_diagnostic = "camera_manager_invalid"; return nil end
    if not object_valid(target) then combat_pawn_diagnostic = "view_target_invalid"; return nil end
    local class_name = safe_call("", function() return target:GetClass():GetFName():ToString() end)
    -- Only a playable party character (PCnnnn_...) counts, never a camera
    -- actor, an enemy or a cinematic proxy.
    if type(class_name) ~= "string" or not class_name:match("^PC%d%d%d%d_") then
        combat_pawn_diagnostic = "view_target_class:" .. tostring(class_name):sub(1, 80)
        return nil
    end
    combat_pawn_diagnostic = "view_target_party:" .. class_name:sub(1, 80)
    return target
end

local function finite_number(value)
    return type(value) == "number" and value == value and value > -1.0e30 and value < 1.0e30
end

local function bounded_text(value, maximum)
    local text = tostring(value or "")
    if #text > maximum then
        return text:sub(1, maximum)
    end
    return text
end

local function json_escape(value)
    local text = bounded_text(value, 512)
    text = text:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\r", "\\r"):gsub("\n", "\\n")
    return '"' .. text .. '"'
end

local function record_capture_failure(reason, detail)
    capture_failures = capture_failures + 1
    consecutive_failures = consecutive_failures + 1
    producer_state = "degraded"
    last_failure_reason = bounded_text(reason or "capture_failed", 96)
    last_failure_detail = bounded_text(detail or "", 512)
end

local function record_schedule_failure(detail)
    schedule_failures = schedule_failures + 1
    consecutive_failures = consecutive_failures + 1
    producer_state = "degraded"
    last_failure_reason = "execute_in_game_thread_failed"
    last_failure_detail = bounded_text(detail or "", 512)
end

local function record_capture_success()
    capture_successes = capture_successes + 1
    consecutive_failures = 0
    producer_state = "running"
    last_success_utc = os.date("!%Y-%m-%dT%H:%M:%SZ")
    last_success_sequence = sequence
end

local function write_health()
    local payload = string.format(
        '{"schema_version":1,"producer":"FF7RTelemetryExporter","scheduler":"native_persistent_game_thread","state":%s,"started_at_utc":%s,"callback_attempts":%d,"capture_successes":%d,"capture_failures":%d,"schedule_failures":%d,"consecutive_failures":%d,"last_attempt_utc":%s,"last_success_utc":%s,"last_success_sequence":%s,"last_failure_reason":%s,"last_failure_detail":%s,"locomotion_diagnostic":%s,"combat_pawn_diagnostic":%s,"periodic_cache_ms":{"menu_last":%.1f,"menu_max":%.1f,"bench_last":%.1f,"bench_max":%.1f},"section_ms_total":{"sample":%.1f,"poi":%.1f,"map_query":%.1f,"route_sweep":%.1f,"write":%.1f,"menu":%.1f,"bench":%.1f,"party":%.1f,"pose":%.1f,"world":%.1f,"battle":%.1f,"locomotion":%.1f,"cutscene":%.1f}}\n',
        json_escape(producer_state),
        json_escape(producer_started_utc),
        callback_attempts,
        capture_successes,
        capture_failures,
        schedule_failures,
        consecutive_failures,
        json_escape(last_attempt_utc),
        json_escape(last_success_utc),
        last_success_sequence and tostring(last_success_sequence) or "null",
        json_escape(last_failure_reason),
        json_escape(last_failure_detail),
        json_escape(locomotion_diagnostic),
        json_escape(combat_pawn_diagnostic),
        periodic_ms.menu_last, periodic_ms.menu_max, periodic_ms.bench_last, periodic_ms.bench_max,
        section_ms.sample, section_ms.poi, section_ms.map_query, section_ms.route_sweep,
        section_ms.write, section_ms.menu, section_ms.bench,
        section_ms.party, section_ms.pose, section_ms.world, section_ms.battle, section_ms.locomotion,
        section_ms.cutscene
    )
    local file = io.open(HEALTH_TEMP_PATH, "wb")
    if not file then return false end
    local ok = pcall(function()
        assert(file:write(payload))
        assert(file:flush())
        assert(file:close())
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
    if force or producer_state ~= last_health_state or health_ticks % 10 == 0 then
        if write_health() then
            last_health_state = producer_state
        end
    end
end

pcall(function() maybe_write_health(true) end)

local function encode_spatial(actor)
    if not object_valid(actor) then
        return ""
    end
    local location = safe_call(nil, function() return actor:K2_GetActorLocation() end)
    local rotation = safe_call(nil, function() return actor:K2_GetActorRotation() end)
    if not location or not rotation then
        return ""
    end
    local values = {
        location.X, location.Y, location.Z,
        rotation.Pitch, rotation.Yaw, rotation.Roll
    }
    for index = 1, 6 do
        if not finite_number(values[index]) then
            return ""
        end
    end
    return string.format("%.6f,%.6f,%.6f,%.6f,%.6f,%.6f",
        values[1], values[2], values[3], values[4], values[5], values[6])
end

-- Capture the physical reference point in the SAME game-thread snapshot as
-- actor pose. Missing reflection stays absent; no guessed capsule dimensions.
local function reflected_read(path, object)
    -- Native C++ accessors are not necessarily UFunctions. Resolve first;
    -- indexing an absent method can throw outside Lua's pcall in this build.
    local method = safe_call(nil, function() return find_object(path) end)
    if not object_valid(method) then
        locomotion_diagnostic = "unavailable:" .. path
        return nil
    end
    return safe_call(nil, function() return method(object) end)
end

local function encode_locomotion(actor)
    locomotion_diagnostic = "actor_unavailable"
    if not object_valid(actor) then return "" end
    local capsule = reflected_read("/Script/Engine.Actor:K2_GetRootComponent", actor)
    if not object_valid(capsule) then return "" end
    local capsule_class = safe_call(nil, function() return find_object("/Script/Engine.CapsuleComponent") end)
    if not object_valid(capsule_class) or not safe_call(false, function() return capsule:IsA(capsule_class) end) then
        locomotion_diagnostic = "root_is_not_capsule"
        return ""
    end
    local center = reflected_read("/Script/Engine.SceneComponent:K2_GetComponentLocation", capsule)
    local up = reflected_read("/Script/Engine.SceneComponent:GetUpVector", capsule)
    local radius = reflected_read("/Script/Engine.CapsuleComponent:GetScaledCapsuleRadius", capsule)
    local half = reflected_read("/Script/Engine.CapsuleComponent:GetScaledCapsuleHalfHeight", capsule)
    local velocity = reflected_read("/Script/Engine.Actor:GetVelocity", actor)
    if not center or not up or not velocity or not finite_number(up.Z) or up.Z < 0.99
        or not finite_number(radius) or radius <= 0 or radius > 200
        or not finite_number(half) or half < radius or half > 300 then return "" end
    local movement = reflected_read("/Script/Engine.Pawn:GetMovementComponent", actor)
    local ground, mode = -1, -1
    if object_valid(movement) then
        local grounded = reflected_read("/Script/Engine.NavMovementComponent:IsMovingOnGround", movement)
        if type(grounded) == "boolean" then ground = grounded and 1 or 0 end
    end
    local values = {center.X, center.Y, center.Z - half, radius, half,
        velocity.X, velocity.Y, velocity.Z, ground, mode}
    for _, value in ipairs(values) do if not finite_number(value) then return "" end end
    locomotion_diagnostic = ground == -1 and "capsule_measured_ground_state_unavailable" or "capsule_and_ground_state_measured"
    return string.format("%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%d,%d", table.unpack(values))
end

local function encode_camera_rotation()
    local controller = safe_call(nil, function() return UEHelpers.GetPlayerController() end)
    local camera = object_valid(controller) and safe_call(nil, function()
        return controller.PlayerCameraManager
    end) or nil
    local rotation = object_valid(camera) and safe_call(nil, function()
        return camera:GetCameraRotation()
    end) or nil
    if not rotation or not finite_number(rotation.Pitch) or
       not finite_number(rotation.Yaw) or not finite_number(rotation.Roll) then
        return ""
    end
    return string.format("%.6f,%.6f,%.6f", rotation.Pitch, rotation.Yaw, rotation.Roll)
end

local function rescan_menu_instances()
    menu_instances = {}
    local objects = FindAllOf("MainMenu_Base_C")
    if objects then
        for _, object in ipairs(objects) do
            if object_valid(object) then
                local full_name = safe_call("", function() return object:GetFullName() end)
                if not full_name:find("Default__", 1, true) then
                    local path = UEHelpers.path(object)
                    if path then menu_instances[#menu_instances + 1] = path end
                end
            end
        end
    end
    menu_ticks_since_scan = 0
end

local function update_menu_cache_on_game_thread()
    menu_ticks_since_scan = menu_ticks_since_scan + 1
    local stale = false
    for _, path in ipairs(menu_instances) do
        local object = UEHelpers.resolve(path)
        if not object_valid(object) then stale = true break end
    end
    local interval = #menu_instances == 0 and MENU_RESCAN_EMPTY_TICKS or MENU_RESCAN_TICKS
    if stale or menu_ticks_since_scan >= interval then
        rescan_menu_instances()
    end
    local found_runtime_instance = false
    for _, path in ipairs(menu_instances) do
        local object = UEHelpers.resolve(path)
        -- Only a shown menu counts: the widget outlives the menu, and
        -- live 2026-09-27 in_menu stayed true in the field after the
        -- menu closed, so routes refused with "gameplay paused".
        if object_valid(object) and safe_call(false, function() return object:IsVisible() end) == true then
            found_runtime_instance = true
            break
        end
    end
    if last_sample_paused ~= 1 then menu_pause_rescanned = false end
    if not found_runtime_instance and last_sample_paused == 1 and not menu_pause_rescanned then
        menu_pause_rescanned = true
        rescan_menu_instances()
        for _, path in ipairs(menu_instances) do
            local object = UEHelpers.resolve(path)
            if object_valid(object) and safe_call(false, function() return object:IsVisible() end) == true then
                found_runtime_instance = true
                break
            end
        end
    end
    cached_in_menu = found_runtime_instance and 1 or 0
end

local function active_field_action_command(any_command)
    local commands = FindAllOf("FieldAction_Command_C")
    if type(commands) ~= "table" or #commands > MAX_COMMAND_WIDGETS then
        return nil, nil
    end

    local text_class = get_api("/Script/EndGame.EndTextBlock")
    local panel_class = get_api("/Script/UMG.PanelWidget")
    local user_widget_class = get_api("/Script/UMG.UserWidget")
    if not text_class or not panel_class or not user_widget_class then
        return nil, nil
    end

    local runtime_commands = {}
    for _, command in ipairs(commands) do
        if object_valid(command) then
            local identity = safe_call("", function() return command:GetFullName() end)
            if identity ~= "" and not identity:find("Default__", 1, true) then
                runtime_commands[#runtime_commands + 1] = command
            end
        end
    end
    local matching_command = nil
    local matching_button = nil
    local matching_widget = nil
    local matching_count = 0
    for _, command in ipairs(runtime_commands) do
        local root = safe_call(nil, function() return command.WidgetTree.RootWidget end)
        if object_valid(root) then
            local queue = {{ widget = root, depth = 0 }}
            local cursor = 1
            local visited = 0
            local seen = {}
            local command_text = nil
            local button_text = nil
            while cursor <= #queue and visited < MAX_COMMAND_TREE_WIDGETS do
                local item = queue[cursor]
                cursor = cursor + 1
                local widget = item.widget
                local identity = safe_call("", function() return widget:GetFullName() end)
                if object_valid(widget) and identity ~= "" and not seen[identity] then
                    seen[identity] = true
                    visited = visited + 1
                    if safe_call(false, function() return widget:IsA(text_class) end) then
                        local value = safe_call("", function() return widget:GetText():ToString() end)
                        if identity:find(".Txt_Command", 1, true) then
                            command_text = value
                        elseif identity:find(".Txt_Btn", 1, true) then
                            button_text = value
                        end
                    end
                    if item.depth < MAX_COMMAND_TREE_DEPTH and
                       safe_call(false, function() return widget:IsA(panel_class) end) then
                        local count = safe_call(-1, function() return widget:GetChildrenCount() end)
                        if type(count) == "number" and count >= 0 and count <= 32 then
                            for index = 0, count - 1 do
                                local child = safe_call(nil, function() return widget:GetChildAt(index) end)
                                if object_valid(child) then
                                    queue[#queue + 1] = { widget = child, depth = item.depth + 1 }
                                end
                            end
                        end
                    end
                    if item.depth < MAX_COMMAND_TREE_DEPTH and
                       safe_call(false, function() return widget:IsA(user_widget_class) end) then
                        local nested = safe_call(nil, function() return widget.WidgetTree.RootWidget end)
                        if object_valid(nested) then
                            queue[#queue + 1] = { widget = nested, depth = item.depth + 1 }
                        end
                    end
                end
            end
            if (command_text == "$level_action_rest" or (any_command and command_text and command_text ~= "")) and
               button_text == "<button=field-action>" then
                matching_count = matching_count + 1
                matching_command = command_text
                matching_button = button_text
                matching_widget = command
            end
        end
    end
    if matching_count ~= 1 then
        return nil, nil
    end
    return matching_command, matching_button, matching_widget
end

-- Benches, like the menu widget, are found by a full object scan (95-330 ms
-- live); only actor paths are retained, and the scan repeats every
-- BENCH_RESCAN_UPDATES checks (about 45 s) to pick up streamed-in areas, or
-- at once when a kept bench unloads.
local bench_instances = nil
local bench_updates_since_scan = 0
local BENCH_RESCAN_UPDATES = 15

local function update_bench_cache_on_game_thread()
    cached_bench = ""
    local bench_class = get_api("/Script/EndGame.EndFieldActionActorBenchBreak")
    local player = safe_call(nil, function()
        local controller = UEHelpers.GetPlayerController()
        return object_valid(controller) and controller.Pawn or nil
    end)
    if not bench_class or not object_valid(player) then
        return
    end

    bench_updates_since_scan = bench_updates_since_scan + 1
    local stale = bench_instances == nil or bench_updates_since_scan >= BENCH_RESCAN_UPDATES
    if not stale then
        for _, path in ipairs(bench_instances) do
            local bench = UEHelpers.resolve(path)
            if not object_valid(bench) then stale = true break end
        end
    end
    if stale then
        local found = FindAllOf("EndFieldActionActorBenchBreak")
        bench_instances = {}
        for _, bench in ipairs(type(found) == "table" and found or {}) do
            local path = UEHelpers.path(bench)
            if path then bench_instances[#bench_instances + 1] = path end
        end
        bench_updates_since_scan = 0
    end
    local benches = bench_instances
    if #benches > 64 then
        return
    end

    local closest = nil
    local closest_distance = nil
    for _, path in ipairs(benches) do
        local bench = UEHelpers.resolve(path)
        local valid_bench = object_valid(bench) and safe_call(false, function()
            local full_name = bench:GetFullName()
            return not full_name:find("Default__", 1, true) and bench:IsA(bench_class)
        end)
        if valid_bench then
            local press_volume = safe_call(nil, function() return bench.PressVolume end)
            local action_type = safe_call(-1, function() return press_volume.ActionType end)
            local distance = safe_call(-1, function() return player:GetDistanceTo(bench) end)
            if object_valid(press_volume) and action_type == 14 and
               finite_number(distance) and distance >= 0 and
               (closest_distance == nil or distance < closest_distance) then
                closest = bench
                closest_distance = distance
            end
        end
    end

    if closest and closest_distance and closest_distance <= BENCH_DETECTION_RADIUS_CM then
        local identity = clean_text(safe_call("", function() return closest:GetFullName() end))
        if identity ~= "" then
            local command_text, button_text = active_field_action_command()
            local closest_usable = closest_distance <= BENCH_USABLE_RADIUS_CM and
                command_text == "$level_action_rest" and
                button_text == "<button=field-action>"
            cached_bench = identity .. "," .. string.format("%.6f", closest_distance / 100.0) ..
                           "," .. (closest_usable and "1" or "0")
            -- The actor pivot is offset from the narrow usable approach lane on
            -- some benches.  Publish the validated PressVolume component centre
            -- so bearing/heading describe the actual interaction geometry.
            local bench_location = safe_call(nil, function()
                return closest.PressVolume:K2_GetComponentLocation()
            end)
            if bench_location and finite_number(bench_location.X) and
               finite_number(bench_location.Y) and finite_number(bench_location.Z) then
                cached_bench = cached_bench .. string.format(",%.6f,%.6f,%.6f",
                    bench_location.X, bench_location.Y, bench_location.Z)
            end
        end
    end
end

local function sample_on_game_thread(generation)
    if generation ~= sample_generation then
        return false, "sample_callback_superseded"
    end
    sequence = sequence + 1
    local party = get_api("/Script/EndGame.Default__EndPartyAPI")
    local menu = get_api("/Script/EndGame.Default__EndMenuBPAPI")
    local battle = get_api("/Script/EndGame.Default__EndBattleAPI")
    local gameplay = get_api("/Script/Engine.Default__GameplayStatics")
    local leader = 16
    local members = {}
    local leader_actor = nil
    local spatial = ""
    local map_id = ""
    local location_id = ""
    local in_combat = -1
    local controller_state = -1
    local controller_actor_id = ""
    local camera_rotation = ""
    local paused = -1
    local target = ""

    local party_started = clock()
    if party and menu then
        leader = safe_call(16, function() return party:GetPartyLeaderType() end)
        local count = safe_call(0, function() return party:GetAllPartyMemberCount() end)
        if type(count) ~= "number" or count < 0 or count > 16 then
            count = 0
        end
        for slot = 0, count - 1 do
            local member_type = safe_call(16, function()
                return party:GetAllPartyMemberType(slot)
            end)
            local member = {
                member_type,
                safe_call(0, function() return menu:BPGetPlayerHP(member_type) end),
                safe_call(0, function() return menu:BPGetPlayerHPMax(member_type) end),
                safe_call(0, function() return menu:BPGetPlayerMP(member_type) end),
                safe_call(0, function() return menu:BPGetPlayerMaxMP(member_type) end)
            }
            members[#members + 1] = table.concat(member, ",")
        end
    end
    timed_section("party", party_started)
    local pose_started = clock()
    local player_controller = safe_call(nil, function() return UEHelpers.GetPlayerController() end)
    leader_actor = safe_call(nil, function()
        if object_valid(player_controller) then
            return player_controller.Pawn
        end
        return nil
    end)
    local controller_valid = object_valid(player_controller)
    spatial = safe_call("", function() return encode_spatial(leader_actor) end)
    camera_rotation = safe_call("", encode_camera_rotation)
    if object_valid(leader_actor) then
        controller_actor_id = clean_text(safe_call("", function()
            return leader_actor:GetFullName()
        end))
    end
    timed_section("pose", pose_started)
    local world_started = clock()
    -- The controller is a valid world context while the pawn is not (battle).
    local world_context = object_valid(leader_actor) and leader_actor or (controller_valid and player_controller or nil)
    if gameplay and world_context then
        map_id = clean_text(safe_call("", function()
            return gameplay:GetCurrentLevelName(world_context, true)
        end))
        paused = safe_call(-1, function()
            return gameplay:IsGamePaused(world_context) and 1 or 0
        end)
    end
    last_sample_paused = paused
    timed_section("world", world_started)
    local battle_started = clock()
    -- One boolean in the existing base tick detects entry/exit. No battle
    -- roster, vitals, target scans or separate combat poller is started here.
    -- Read it with a loaded party and a world, never from the title screen
    -- (per-tick game API calls there crashed the game, 2026-10-05).
    local combat_pawn = nil
    combat_pawn_source = "none"
    if battle and world_context and (object_valid(leader_actor) or #members > 0) then
        local active = safe_call(nil, function() return battle:IsInBattle() end)
        if type(active) == "boolean" then in_combat = active and 1 or 0 end
    end
    if in_combat == 1 then
        if object_valid(leader_actor) then
            combat_pawn, combat_pawn_source = leader_actor, "controller_pawn"
        else
            combat_pawn = battle_view_pawn(player_controller)
            if combat_pawn then
                combat_pawn_source = "camera_view_target"
            elseif party then
                -- The typed battle-party member of the party leader's type.
                local count = safe_call(0, function() return party:GetBattleMemberCount() end)
                if type(count) == "number" and count > 0 and count <= 8 then
                    for slot = 0, count - 1 do
                        local member_type = safe_call(nil, function() return party:GetBattleMemberType(slot) end)
                        if member_type == leader then
                            local member = safe_call(nil, function() return party:GetBattleMember(slot) end)
                            if object_valid(member) then
                                combat_pawn, combat_pawn_source = member, "battle_party_leader"
                                combat_pawn_diagnostic = combat_pawn_diagnostic .. ";battle_party_leader"
                            end
                            break
                        end
                    end
                end
            end
            if combat_pawn then
                controller_actor_id = clean_text(safe_call("", function() return combat_pawn:GetFullName() end))
                -- The battle pawn stands in for the missing controller pawn:
                -- its position, state and target are the player's in battle
                -- (live 2026-10-20: input was refused without them).
                spatial = safe_call("", function() return encode_spatial(combat_pawn) end)
            end
        end
    end
    local battle_actor = object_valid(leader_actor) and leader_actor or combat_pawn
    if battle and object_valid(battle_actor) then
        controller_state = safe_call(-1, function()
            return battle:GetCharacterStateType(battle_actor)
        end)
        if type(controller_state) ~= "number" or controller_state < 0 or controller_state > 39 then
            controller_state = -1
        end
        target = safe_call("", function()
            local target_actor = battle:GetTargetCharacter(battle_actor)
            if object_valid(target_actor) then
                local target_name = clean_text(safe_call("", function()
                    return target_actor:GetFullName()
                end))
                local target_distance = safe_call(-1, function()
                    return battle_actor:GetDistanceTo(target_actor)
                end)
                if finite_number(target_distance) and target_distance >= 0 then
                    return target_name .. "," .. string.format("%.6f", target_distance)
                end
            end
            return ""
        end)
    end

    timed_section("battle", battle_started)
    local locomotion_started = clock()
    local locomotion = safe_call("", function() return encode_locomotion(leader_actor) end)
    timed_section("locomotion", locomotion_started)
    local source_timestamp = nil
    local poi_started = clock()
    if entity_poi_ok then
        local poi_ok, poi_error = pcall(entity_poi.sample, sequence, leader_actor, map_id, active_field_action_command, source_timestamp)
        if poi_ok and type(poi_error) == "string" then source_timestamp = poi_error end
        if not poi_ok and producer_tick % 10 == 1 then
            local diagnostic = io.open("C:\\CaitGPT\\Telemetry\\entity-poi-error.txt", "wb")
            if diagnostic then diagnostic:write(tostring(poi_error));diagnostic:close() end
        end
    end
    timed_section("poi", poi_started)
    source_timestamp = source_timestamp or os.date("!%Y-%m-%dT%H:%M:%SZ")
    local map_started = clock()
    if map_query_ok then
        -- Idle unless the agent has written map-query.request; see map_query.lua.
        local map_ok, map_error = pcall(map_query.sample, sequence, leader_actor, map_id, paused, source_timestamp)
        if not map_ok and producer_tick % 10 == 1 then
            local diagnostic = io.open("C:\\CaitGPT\\Telemetry\\map-query-error.txt", "wb")
            if diagnostic then diagnostic:write(tostring(map_error));diagnostic:close() end
        end
    end
    timed_section("map_query", map_started)
    -- Idle except for an explicit read-only objective request.
    local objective_started = clock()
    if objective_query_ok then
        pcall(objective_query.sample, sequence, leader_actor, map_id, source_timestamp)
    end
    timed_section("objective_query", objective_started)
    -- Disable the detailed reader on exit or unknown state. Outside combat
    -- even its marker-file checks are off; rosters are read only on demand.
    if combat_query_ok then
        local active = in_combat == 1
        if active ~= combat_query_active then
            combat_query_active = active
            pcall(combat_query.set_active, active)
        end
        if combat_query_active then
            local combat_started = clock()
            pcall(combat_query.sample, sequence, combat_pawn, map_id, combat_pawn_source)
            timed_section("combat_query", combat_started)
        end
    end
    local sweep_started = clock()
    if route_sweep_ok then
        -- Idle unless the agent has written route-sweep.request; see route_sweep.lua.
        local sweep_ok, sweep_error = pcall(route_sweep.sample, sequence, leader_actor, map_id, source_timestamp)
        if not sweep_ok and producer_tick % 10 == 1 then
            local diagnostic = io.open("C:/CaitGPT/Telemetry/route-sweep-error.txt", "wb")
            if diagnostic then diagnostic:write(tostring(sweep_error));diagnostic:close() end
        end
    end
    timed_section("route_sweep", sweep_started)
    local cutscene_started = clock()
    if cutscene_ok then
        -- Writes cutscene.state when a sequence starts or stops; see cutscene.lua.
        local scene_ok, scene_error = pcall(cutscene.sample, sequence, leader_actor, source_timestamp)
        if not scene_ok and producer_tick % 10 == 1 then
            local diagnostic = io.open("C:\\CaitGPT\\Telemetry\\cutscene-error.txt", "wb")
            if diagnostic then diagnostic:write(tostring(scene_error));diagnostic:close() end
        end
    end
    timed_section("cutscene", cutscene_started)
    local line = table.concat({
        tostring(sequence),
        source_timestamp,
        tostring(leader),
        tostring(#members),
        table.concat(members, ";"),
        spatial,
        map_id,
        location_id,
        tostring(in_combat),
        tostring(paused),
        target,
        tostring(cached_in_menu),
        cached_bench,
        tostring(controller_state),
        controller_actor_id,
        camera_rotation,
        locomotion
    }, "|") .. "\n"

    if generation ~= sample_generation then
        return false, "sample_callback_superseded"
    end
    local write_started = clock()
    local file = io.open(SAMPLE_PATH, "wb")
    if not file then
        return false, "state_sample_open_failed"
    end
    local ok, detail = pcall(function()
        assert(file:write(line))
        assert(file:flush())
        assert(file:close())
    end)
    timed_section("write", write_started)
    if not ok then
        pcall(function() file:close() end)
        return false, bounded_text(detail or "state_sample_write_failed", 512)
    end
    return true
end

-- The paired native DLL calls this synchronously on its verified game thread.
-- No asynchronous work is queued; a failed sample is retried next period.
function FF7RReadOnlyTick()
    producer_tick = producer_tick + 1
    callback_attempts = callback_attempts + 1
    last_attempt_utc = os.date("!%Y-%m-%dT%H:%M:%SZ")
    sample_generation = sample_generation + 1
    local generation = sample_generation
    local ok, detail = pcall(function()
        if producer_tick % 10 == 0 then
            local menu_started = clock()
            local menu_ok, menu_detail = pcall(update_menu_cache_on_game_thread)
            periodic_ms.menu_last = (clock() - menu_started) * 1000
            periodic_ms.menu_max = math.max(periodic_ms.menu_max, periodic_ms.menu_last)
            section_ms.menu = section_ms.menu + periodic_ms.menu_last
            if not menu_ok and not reported_menu_cache_error then
                reported_menu_cache_error = true
                print("[FF7RTelemetryExporter] menu cache error: " .. tostring(menu_detail) .. "\n")
            end
        end
        if producer_tick % 20 == 0 then
            local bench_started = clock()
            local bench_ok, bench_detail = pcall(update_bench_cache_on_game_thread)
            periodic_ms.bench_last = (clock() - bench_started) * 1000
            periodic_ms.bench_max = math.max(periodic_ms.bench_max, periodic_ms.bench_last)
            section_ms.bench = section_ms.bench + periodic_ms.bench_last
            if not bench_ok then
                cached_bench = ""
                if not reported_bench_cache_error then
                    reported_bench_cache_error = true
                    print("[FF7RTelemetryExporter] bench cache error: " .. tostring(bench_detail) .. "\n")
                end
            end
        end
        local sample_started = clock()
        local sample_ok, sample_detail = sample_on_game_thread(generation)
        timed_section("sample", sample_started)
        if sample_ok == true then
            record_capture_success()
        else
            record_capture_failure(sample_detail or "sample_capture_failed", sample_detail)
        end
    end)
    if not ok then record_capture_failure("sample_callback_exception", detail) end
    pcall(function() maybe_write_health(false) end)
end
