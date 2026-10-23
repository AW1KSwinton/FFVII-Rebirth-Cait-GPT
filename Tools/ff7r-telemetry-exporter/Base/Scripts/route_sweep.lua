-- On-demand route collision audit (read-only; no input, no state change).
--
-- The navmesh does not contain placed props: live 2026-09-26 at Kalm a
-- merchant wagon stood on one walkable polygon and the route follower walked
-- into it. Before moving, the agent asks this module to sweep Cloud's capsule
-- along the planned route with the engine's own collision, and gets every
-- blocker with the actor and component that was hit, whether the component
-- simulates physics (a chair Cloud nudges aside) and its bounds.
--
-- Request (route-sweep.request, text):
--   <id>|<expires unix>|<radius cm>|<half height cm>|<lift cm>[|<query>[|<max step cm>]]
--   query: profile (default, Cloud's collision profile), channel:<n>
--   (a trace channel) or objects:<n>,<n> (object types), for diagnosis
--
-- The capsule follows the floor, as Cloud does: each segment is walked in
-- 50 cm steps, the floor under each step found by a downward trace, and the
-- capsule swept from floor to floor. Sweeping the straight line between
-- waypoints instead floated over a knee-high stair side wall at Kalm that
-- Cloud, on the landing below, walked into. A floor rise above the step
-- height is reported as a ledge, a fall of over 1.5 m as a drop.
--   <x>,<y>,<z>        feet positions, one per line, 2..128 points
-- Response: route-sweep.json. Breadcrumbs: route-sweep-trace.txt.

local UEHelpers = require("object_refs")
local mq_ok, mq = pcall(require, "map_query")

local M = {}
local ROOT = "C:\\CaitGPT\\Telemetry\\"
local REQUEST_PATH = ROOT .. "route-sweep.request"
local RESPONSE_PATH = ROOT .. "route-sweep.json"
local RESPONSE_TMP_PATH = ROOT .. "route-sweep.json.tmp"
local TRACE_PATH = ROOT .. "route-sweep-trace.txt"
local CHECK_EVERY_TICKS = 3
local MAX_POINTS = 128
local MAX_HITS_PER_SEGMENT = 6
local MAX_REQUEST_BYTES = 8192
-- Sweeps use Cloud's own collision profile, so they collide with what his
-- movement collides with. The Visibility channel (0, the navigation probe's)
-- was tried first live and hit grass and glass while missing the café
-- chairs and table; it is only the fallback when no profile is readable.
local TRACE_CHANNEL = 0
local SUBSTEP_CM = 50
local FLOOR_PROBE_UP_CM = 60
local FLOOR_PROBE_DOWN_CM = 300
local MAX_DROP_CM = 150
-- A rise past the step limit within one 50 cm step is re-sampled this many
-- times: stairs climb in risers no taller than STAIR_RISE_CM, a ledge in one
-- (live at Kalm: the steps up to Nene rose 45-50 cm per step and were read
-- as ledges, walling the stairs off).
local STAIR_SUBSTEPS = 4
local STAIR_RISE_CM = 30
local DEFAULT_MAX_STEP_CM = 45

local handled_request_id = nil
local ticks = 0

local function trace(stage)
    pcall(function()
        local f = io.open(TRACE_PATH, 'ab')
        if f then f:write(os.date('!%H:%M:%S') .. ' ' .. tostring(stage) .. '\n'); f:close() end
    end)
end

local function safe(f, fallback)
    local ok, value = pcall(f)
    if ok and value ~= nil then return value end
    return fallback
end

local function number(v)
    return type(v) == "number" and v == v and math.abs(v) < 1e12
end

local function vec3(v)
    local x = safe(function() return v.X end, nil)
    local y = safe(function() return v.Y end, nil)
    local z = safe(function() return v.Z end, nil)
    if number(x) and number(y) and number(z) then return {x = x, y = y, z = z} end
    return nil
end

local function valid(object)
    return safe(function() return object ~= nil and object:IsValid() end, false)
end

local function full_name(object)
    return safe(function() return tostring(object:GetFullName()) end, nil)
end

local function load_request()
    local f = io.open(REQUEST_PATH, 'rb')
    if not f then return nil end
    local s = f:read(MAX_REQUEST_BYTES + 1)
    f:close()
    if not s or #s > MAX_REQUEST_BYTES then return nil end
    local lines = {}
    for line in s:gmatch('[^\r\n]+') do lines[#lines + 1] = line end
    local header = {}
    for field in (lines[1] or ''):gmatch('([^|]+)') do header[#header + 1] = field end
    if #header < 5 or #header > 7 then return nil end
    local max_step = tonumber(header[7] or DEFAULT_MAX_STEP_CM)
    if not (number(max_step) and max_step > 0 and max_step < 200) then return nil end
    local query = header[6] or 'profile'
    local query_kind, query_values = query:match('^(%a+):?([%d,]*)$')
    if query_kind ~= 'profile' and not ((query_kind == 'channel' or query_kind == 'objects') and query_values ~= '') then
        return nil
    end
    local numbers = {}
    for v in query_values:gmatch('%d+') do numbers[#numbers + 1] = tonumber(v) end
    local id = header[1]
    local expires, radius, half_height, lift = tonumber(header[2]), tonumber(header[3]), tonumber(header[4]), tonumber(header[5])
    if not id:match('^%w+$') or #id > 64 then return nil end
    local now = os.time()
    if not expires or expires < now or expires > now + 600 then return nil end
    if not (number(radius) and radius > 0 and radius < 200 and number(half_height) and half_height > 0
            and half_height < 300 and number(lift) and lift >= 0 and lift < 200) then return nil end
    local points = {}
    for i = 2, #lines do
        local x, y, z = lines[i]:match('^%s*([%-%d%.eE]+),([%-%d%.eE]+),([%-%d%.eE]+)%s*$')
        x, y, z = tonumber(x), tonumber(y), tonumber(z)
        if not (number(x) and number(y) and number(z)) then return nil end
        points[#points + 1] = {X = x, Y = y, Z = z}
    end
    if #points < 2 or #points > MAX_POINTS then return nil end
    return {id = id, radius = radius, half_height = half_height, lift = lift, points = points,
            query_kind = query_kind, query_numbers = numbers, max_step = max_step}
end

-- FHitResult.Actor / .Component are weak pointers; UE4SS may hand back the
-- object or a wrapper with Get(). Record which form worked.
local function weak_object(value, access)
    if valid(value) then access.direct = (access.direct or 0) + 1; return value end
    local got = safe(function() return value:Get() end, nil)
    if valid(got) then access.get = (access.get or 0) + 1; return got end
    access.missing = (access.missing or 0) + 1
    return nil
end

-- Through the engine function: USceneComponent::Bounds is not a reflected
-- property, and blind property reads have aborted sampler calls past pcall.
local function bounds_of(component)
    local origin, extent, radius = {}, {}, {}
    local ok = pcall(function()
        UEHelpers.GetKismetSystemLibrary():GetComponentBounds(component, origin, extent, radius)
    end)
    if not ok then return nil end
    local o, e = vec3(origin), vec3(extent)
    if o == nil or e == nil then return nil end
    return {origin = o, extent = e}
end

local function actor_bounds(actor)
    local origin, extent = {}, {}
    local ok = pcall(function() actor:GetActorBounds(true, origin, extent, false) end)
    if not ok then return nil end
    local o, e = vec3(origin), vec3(extent)
    if o == nil or e == nil then return nil end
    return {origin = o, extent = e}
end

local detail_traces = 0
local function dtrace(stage)
    if detail_traces < 40 then detail_traces = detail_traces + 1; trace(stage) end
end

local function describe_hit(hit, access)
    dtrace('hit: scalars')
    local result = {
        distance_cm = safe(function() return hit.Distance end, nil),
        impact = vec3(safe(function() return hit.ImpactPoint end, nil)),
        location = vec3(safe(function() return hit.Location end, nil)),
        normal_z = safe(function() return hit.ImpactNormal.Z end, nil),
        start_penetrating = safe(function() return hit.bStartPenetrating end, nil),
    }
    dtrace('hit: actor')
    local actor = weak_object(safe(function() return hit.Actor end, nil), access)
    dtrace('hit: component')
    local component = weak_object(safe(function() return hit.Component end, nil), access)
    dtrace('hit: actor details')
    if actor ~= nil then
        result.actor = full_name(actor)
        result.actor_class = safe(function() return tostring(actor:GetClass():GetFName():ToString()) end, nil)
        result.actor_bounds = actor_bounds(actor)
    end
    dtrace('hit: component details')
    if component ~= nil then
        result.component = full_name(component)
        result.simulating_physics = safe(function() return component:IsAnySimulatingPhysics() end, nil)
        result.mobility = safe(function() return tonumber(component.Mobility) end, nil)
        result.component_bounds = bounds_of(component)
    end
    return result, actor
end

local function collision_profile(pawn)
    local root = safe(function() return pawn:K2_GetRootComponent() end, nil)
    if not valid(root) then return nil end
    local profile = safe(function() return root:GetCollisionProfileName() end, nil)
    local name = safe(function() return profile:ToString() end, nil)
    if profile == nil or name == nil or name == '' or name == 'None' then return nil end
    return profile, name
end

local function sweep(world, pawn, request, a, b, ignore, query)
    local lift = request.half_height + request.lift
    local start_v = {X = a.X, Y = a.Y, Z = a.Z + lift}
    local end_v = {X = b.X, Y = b.Y, Z = b.Z + lift}
    local hit = {}
    local ok, was_hit = pcall(function()
        local kismet = UEHelpers.GetKismetSystemLibrary()
        if query.objects ~= nil then
            return kismet:CapsuleTraceSingleForObjects(
                world, start_v, end_v, request.radius, request.half_height, query.objects, false,
                ignore, 0, hit, true, {R=0,G=128,B=255,A=255}, {R=255,G=128,B=0,A=255}, 0.0)
        end
        if query.profile ~= nil then
            return kismet:CapsuleTraceSingleByProfile(
                world, start_v, end_v, request.radius, request.half_height, query.profile, false,
                ignore, 0, hit, true, {R=0,G=128,B=255,A=255}, {R=255,G=128,B=0,A=255}, 0.0)
        end
        return kismet:CapsuleTraceSingle(
            world, start_v, end_v, request.radius, request.half_height, query.channel or TRACE_CHANNEL, false,
            ignore, 0, hit, true, {R=0,G=128,B=255,A=255}, {R=255,G=128,B=0,A=255}, 0.0)
    end)
    if not ok then return nil, 'capsule_trace_unavailable: ' .. tostring(was_hit) end
    if not was_hit then return false end
    return hit
end

local function floor_at(pawn, query, x, y, z_hint, ignore)
    local start_v = {X = x, Y = y, Z = z_hint + FLOOR_PROBE_UP_CM}
    local end_v = {X = x, Y = y, Z = z_hint - FLOOR_PROBE_DOWN_CM}
    local hit = {}
    local ok, was_hit = pcall(function()
        local kismet = UEHelpers.GetKismetSystemLibrary()
        if query.objects ~= nil then
            return kismet:LineTraceSingleForObjects(pawn, start_v, end_v, query.objects, false, ignore, 0, hit,
                true, {R=0,G=255,B=0,A=255}, {R=255,G=0,B=0,A=255}, 0.0)
        end
        if query.profile ~= nil then
            return kismet:LineTraceSingleByProfile(pawn, start_v, end_v, query.profile, false, ignore, 0, hit,
                true, {R=0,G=255,B=0,A=255}, {R=255,G=0,B=0,A=255}, 0.0)
        end
        return kismet:LineTraceSingle(pawn, start_v, end_v, query.channel or TRACE_CHANNEL, false, ignore, 0, hit,
            true, {R=0,G=255,B=0,A=255}, {R=255,G=0,B=0,A=255}, 0.0)
    end)
    if not ok or not was_hit then return nil end
    local z = safe(function() return hit.ImpactPoint.Z end, nil)
    if not number(z) then return nil end
    return z
end

-- A floor this far under the route is the probe missing the floor Cloud
-- walks on: slipping between planks, or a walkway these traces do not see
-- at all (live 2026-10-06 at Cosmo Canyon: 2.75 m down along a 2 m stretch
-- the navmesh crosses, then back to the route, read as a drop and a ledge
-- that walled off Robin and Ka'dina). Probes a hand's width either side
-- find boards; with none, the navmesh the route follows (built from the
-- level's collision, so never over a hole) gives the floor.
local GAP_RESAMPLE_CM = 15
-- Down stairs the straight route sits at most about a step over the treads;
-- a floor further under it than this is not what Cloud walks on (live
-- 2026-10-06 on the way to Bernard: 138 cm under the route, read as a
-- 152 cm drop that every re-plan crossed).
local UNSEEN_BELOW_ROUTE_CM = 100

local function floor_across_gap(pawn, query, x, y, z_hint, expected, ignore, ux, uy)
    local best = nil
    for _, offset in ipairs({{-uy, ux}, {uy, -ux}, {ux, uy}, {-ux, -uy}}) do
        local z = floor_at(pawn, query, x + offset[1] * GAP_RESAMPLE_CM, y + offset[2] * GAP_RESAMPLE_CM, z_hint, ignore)
        if z ~= nil and math.abs(z - expected) <= UNSEEN_BELOW_ROUTE_CM
            and (best == nil or math.abs(z - expected) < math.abs(best - expected)) then
            best = z
        end
    end
    return best
end

-- The floor between two points in short steps, or nil where it rises
-- abruptly (a ledge) or cannot be found.
local function stair_path(pawn, query, from, to, ignore)
    local points, last = {}, from
    for k = 1, STAIR_SUBSTEPS do
        local t = k / STAIR_SUBSTEPS
        local x, y = from.X + (to.X - from.X) * t, from.Y + (to.Y - from.Y) * t
        local z = to.Z
        if k < STAIR_SUBSTEPS then
            z = floor_at(pawn, query, x, y, math.max(last.Z, from.Z + (to.Z - from.Z) * t), ignore)
        end
        if z == nil or z - last.Z > STAIR_RISE_CM then return nil end
        last = {X = x, Y = y, Z = z}
        points[#points + 1] = last
    end
    return points
end

local function audit(request, pawn)
    local access = {}
    local segments = {}
    local query, described = {}, {}
    if request.query_kind == 'channel' then
        query.channel = request.query_numbers[1]
        described.channel = query.channel
    elseif request.query_kind == 'objects' then
        query.objects = request.query_numbers
        described.objects = mq.array(request.query_numbers)
    else
        local profile, profile_name = collision_profile(pawn)
        query.profile = profile
        if profile_name then described.profile = profile_name else described.channel = TRACE_CHANNEL end
    end
    trace('query ' .. mq.json(described))
    local ignore_base = {pawn}
    local floor_missing = 0
    local floor_unseen = 0
    -- The floor under a route point, from a probe or (see GAP_RESAMPLE_CM)
    -- from the route itself.
    local function route_floor(x, y, z_hint, expected, ux, uy)
        local floor = floor_at(pawn, query, x, y, z_hint, ignore_base)
        if floor ~= nil and expected - floor > UNSEEN_BELOW_ROUTE_CM then
            floor = floor_across_gap(pawn, query, x, y, z_hint, expected, ignore_base, ux, uy)
            if floor == nil then floor_unseen = floor_unseen + 1; floor = expected end
        end
        return floor
    end
    for index = 1, #request.points - 1 do
        local a, b = request.points[index], request.points[index + 1]
        local ignore = {}
        for i, v in ipairs(ignore_base) do ignore[i] = v end
        local hits = {}
        local error = nil
        local length = math.sqrt((b.X - a.X) ^ 2 + (b.Y - a.Y) ^ 2)
        local steps = math.max(1, math.ceil(length / SUBSTEP_CM))
        local ux, uy = 0, 0
        if length > 0 then ux, uy = (b.X - a.X) / length, (b.Y - a.Y) / length end
        -- Capsule hits are ignored on later steps to enumerate other blockers.
        -- Floor probes must still see those actors: a hit on a merged level
        -- tile otherwise removes its stairs from every later floor probe.
        local previous = {X = a.X, Y = a.Y, Z = route_floor(a.X, a.Y, a.Z, a.Z, ux, uy) or a.Z}
        for step = 1, steps do
            if #hits >= MAX_HITS_PER_SEGMENT or error ~= nil then break end
            local t = step / steps
            local expected = a.Z + (b.Z - a.Z) * t
            local x, y = a.X + (b.X - a.X) * t, a.Y + (b.Y - a.Y) * t
            local floor = route_floor(x, y, math.max(previous.Z, expected), expected, ux, uy)
            if floor == nil then floor_missing = floor_missing + 1; floor = expected end
            local current = {X = x, Y = y, Z = floor}
            local rise = floor - previous.Z
            -- The capsule follows stairs riser by riser; one sweep across a
            -- whole 50 cm step would cut through the step noses.
            local path = {current}
            -- A ledge leaves the floor above the route, as on a prop the navmesh
            -- omits; a rise back to the route's own height is the walkway after
            -- readings below it (live 2026-10-06 at Cosmo Canyon: 149 cm back up
            -- after a reading 1 cm short of the unseen-floor limit).
            if rise > request.max_step and floor - expected > request.max_step then
                local stairs = stair_path(pawn, query, previous, current, ignore_base)
                if stairs ~= nil then
                    path = stairs
                else
                    hits[#hits + 1] = {ledge = true, rise_cm = rise, normal_z = 0,
                                       impact = {x = x, y = y, z = floor}, distance_cm = length * t,
                                       route_z = expected, previous_floor_z = previous.Z, step = step, steps = steps}
                end
            elseif -rise > MAX_DROP_CM then
                hits[#hits + 1] = {drop = true, rise_cm = rise, normal_z = 0,
                                   impact = {x = x, y = y, z = floor}, distance_cm = length * t,
                                   route_z = expected, previous_floor_z = previous.Z, step = step, steps = steps}
            end
            local from = previous
            for _, point in ipairs(path) do
                local hit, failure = sweep(pawn, pawn, request, from, point, ignore, query)
                if hit == nil then
                    error = failure
                    break
                elseif hit ~= false then
                    local described, actor = describe_hit(hit, access)
                    described.distance_cm = length * (step - 1) / steps + (described.distance_cm or 0)
                    hits[#hits + 1] = described
                    -- Later steps look past this blocker, to list every one on the segment.
                    if actor ~= nil then ignore[#ignore + 1] = actor end
                end
                from = point
            end
            previous = current
        end
        segments[#segments + 1] = {index = index - 1, hits = mq.array(hits), error = error,
                                   complete = #hits < MAX_HITS_PER_SEGMENT and error == nil}
        if index % 8 == 0 then trace('segment ' .. index) end
    end
    return {status = 'ok', segments = mq.array(segments), hit_object_access = access, query = described,
            floor_missing = floor_missing, floor_unseen = floor_unseen}
end

-- The response is replaced by delete-and-rename, which Windows refuses
-- while the agent has the file open to read it (live: "written ok=true
-- result=false" and the request never answered). A written response whose
-- rename failed is kept and published again on the following ticks.
local pending_publish = false

local function publish_response()
    os.remove(RESPONSE_PATH)
    pending_publish = os.rename(RESPONSE_TMP_PATH, RESPONSE_PATH) == nil
    return not pending_publish
end

local function write_response(payload)
    local encoded = mq.json(payload)
    local f = io.open(RESPONSE_TMP_PATH, 'wb')
    if not f then return false end
    local ok = pcall(function() f:write(encoded); f:flush(); f:close() end)
    if not ok then pcall(function() f:close() end); return false end
    return publish_response()
end

function M.sample(sequence, player, map_id, source_timestamp)
    if not mq_ok then return end
    ticks = ticks + 1
    if pending_publish then
        local published = publish_response()
        trace('republished result=' .. tostring(published))
    end
    if ticks % CHECK_EVERY_TICKS ~= 0 then return end
    local request = load_request()
    if request == nil or request.id == handled_request_id then return end
    handled_request_id = request.id
    detail_traces = 0
    trace('request ' .. request.id .. ' points=' .. #request.points)
    local ok, payload = pcall(audit, request, player)
    trace('audit ok=' .. tostring(ok) .. (ok and '' or (' ' .. tostring(payload))))
    if not ok then payload = {status = 'error', error = tostring(payload)} end
    payload.schema_version = 1
    payload.source = 'ue4ss_route_sweep'
    payload.request_id = request.id
    payload.source_sequence = sequence
    payload.timestamp_utc = source_timestamp
    payload.map_id = map_id
    payload.capsule = {radius_cm = request.radius, half_height_cm = request.half_height, lift_cm = request.lift}
    local written_ok, written = pcall(write_response, payload)
    trace('written ok=' .. tostring(written_ok) .. ' result=' .. tostring(written))
    if not written_ok or written ~= true then
        pcall(write_response, {schema_version = 1, source = 'ue4ss_route_sweep', request_id = request.id,
                               status = 'error', error = 'response_write_failed: ' .. tostring(written)})
    end
end

return M
