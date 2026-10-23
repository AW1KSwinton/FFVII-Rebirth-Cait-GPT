-- Executed in Lua 5.4 with mock UE4SS objects and an in-memory filesystem:
--   lua route_sweep_regression.lua ..\Scripts\route_sweep.lua ..\Scripts\map_query.lua
-- A chair that simulates physics and a fixed wagon stand on the first route
-- segment; the second segment is clear.
local source, map_query_source = assert(arg[1]), assert(arg[2])
local files, now = {}, 100
local root = 'C:\\CaitGPT\\Telemetry\\'
local env = setmetatable({}, {__index = _G})

local function object(name, class, fields)
    local value = fields or {}
    value.IsValid = function() return true end
    value.GetFullName = function() return name end
    value.GetClass = function() return {GetFName = function() return {ToString = function() return class end} end} end
    return value
end

local function prop(name, x, simulating)
    local component = object('StaticMeshComponent ' .. name .. '.Mesh', 'StaticMeshComponent', {
        IsAnySimulatingPhysics = function() return simulating end,
        Mobility = 2,
    })
    local actor = object('BP_Prop_C ' .. name, 'BP_Prop_C', {
        GetActorBounds = function(_, _, origin, extent)
            origin.X, origin.Y, origin.Z = x, 0, 50
            extent.X, extent.Y, extent.Z = 40, 40, 50
        end,
    })
    return {x = x, actor = actor, component = component}
end
local props = {prop('Chair', 300, true), prop('Wagon', 600, false)}
local pawn = object('PC0000_00_Cloud_Standard_C Cloud', 'PC0000_00_Cloud_Standard_C', {
    K2_GetRootComponent = function() return object('EndCharacterRootComponent Cloud.Root', 'EndCharacterRootComponent', {
        GetCollisionProfileName = function() return {ToString = function() return 'EndPlayer' end} end,
    }) end,
})

-- Flat floor at z 10, except a 70 cm ledge on the second segment (y >= 300)
-- and, far off (y <= -400), stairs from x 2000: 25 cm risers every 25 cm.
local function floor_z(x, y)
    -- Boards at y 750-850 with a 10 cm gap at x 495-505 over ground 3 m down,
    -- and at y 950-1050 a 2 m walkway the probes cannot see at x 300-500.
    if y >= 750 and y <= 850 and x >= 495 and x <= 505 then return -290 end
    if y >= 950 and y <= 1050 and x >= 300 and x <= 500 then return -265 end
    -- y 1150-1250: readings 1.4 m low at x 300-500, under the unseen-floor limit.
    if y >= 1150 and y <= 1250 and x >= 300 and x <= 500 then return -130 end
    -- y 1350-1450: floor 20 cm over the route, then reading 1.38 m under it at x 300-500.
    if y >= 1350 and y <= 1450 and x >= 300 and x <= 500 then return 10 - 138 end
    if y >= 1350 and y <= 1450 and x < 300 then return 30 end
    if y <= -400 and x >= 2000 then return 10 + math.min(10, math.floor((x - 2000) / 25)) * 25 end
    if x >= 999 and y >= 300 then return 80 end
    return 10
end
local sweeps = 0
local floor_probes = 0
local profiles = {}
local kismet = {}
local floor_actor = object('CollisionAssetActor LevelTile', 'CollisionAssetActor')
local floor_component = object('CollisionAssetComponent LevelTile.Mesh', 'CollisionAssetComponent', {Mobility = 0})
local hit_floor_tile = false
local function floor_trace(start_v, end_v, hit, ignore)
    floor_probes = floor_probes + 1
    for _, actor in ipairs(ignore) do
        if actor == floor_actor then return false end
    end
    assert(start_v.X == end_v.X and start_v.Y == end_v.Y and start_v.Z > end_v.Z, 'floor probe not vertical')
    hit.ImpactPoint = {X = start_v.X, Y = start_v.Y, Z = floor_z(start_v.X, start_v.Y)}
    return true
end
kismet.LineTraceSingleByProfile = function(_, world, s, e, profile, complex, ignore, draw, hit) return floor_trace(s, e, hit, ignore) end
kismet.LineTraceSingle = function(_, world, s, e, channel, complex, ignore, draw, hit) return floor_trace(s, e, hit, ignore) end
kismet.LineTraceSingleForObjects = function(_, world, s, e, objects, complex, ignore, draw, hit) return floor_trace(s, e, hit, ignore) end
kismet.CapsuleTraceSingleByProfile = function(self, world, start_v, end_v, radius, half_height, profile, ...)
    profiles[#profiles + 1] = profile:ToString()
    return self.CapsuleTraceSingle(self, world, start_v, end_v, radius, half_height, 0, ...)
end
kismet.CapsuleTraceSingle = function(_, world, start_v, end_v, radius, half_height, channel, complex, ignore, draw, hit)
        sweeps = sweeps + 1
        assert(world == pawn, 'sweep not in the pawn world')
        local feet = start_v.Z - half_height - 45
        assert(feet == 10 or feet == 80 or start_v.Y <= -400 or start_v.Y >= 1150, 'capsule not lifted to step height above the floor')
        local skip = {}
        for _, v in ipairs(ignore) do skip[v] = true end
        if hit_floor_tile and not skip[floor_actor] then
            hit.Distance = 0
            hit.ImpactPoint = {X = start_v.X, Y = start_v.Y, Z = start_v.Z - half_height - 45}
            hit.Location = start_v
            hit.ImpactNormal = {X = 0, Y = 0, Z = 0}
            hit.bStartPenetrating = true
            hit.Actor, hit.Component = floor_actor, floor_component
            return true
        end
        for _, p in ipairs(props) do
            if not skip[p.actor] and p.x >= math.min(start_v.X, end_v.X) and p.x <= math.max(start_v.X, end_v.X) then
                hit.Distance = p.x - start_v.X - radius
                hit.ImpactPoint = {X = p.x - 40, Y = 0, Z = start_v.Z}
                hit.Location = {X = p.x - 40 - radius, Y = 0, Z = start_v.Z}
                hit.ImpactNormal = {X = -1, Y = 0, Z = 0}
                hit.bStartPenetrating = false
                hit.Actor = {Get = function() return p.actor end} -- weak pointer wrapper
                hit.Component = p.component
                return true
            end
        end
        return false
    end
kismet.GetComponentBounds = function(_, component, origin, extent)
        for _, p in ipairs(props) do
            if p.component == component then
                origin.X, origin.Y, origin.Z = p.x, 0, 50
                extent.X, extent.Y, extent.Z = 35, 35, 45
            end
        end
    end
local helpers = {GetKismetSystemLibrary = function() return kismet end}

env.os = {
    time = function() return now end,
    date = os.date,
    remove = function(path) files[path] = nil end,
    rename = function(from, to) files[to] = files[from]; files[from] = nil; return true end,
}
env.io = {open = function(path, mode)
    if mode == 'rb' then
        if not files[path] then return nil end
        return {read = function() return files[path] end, close = function() end}
    end
    if mode == 'ab' then return {write = function() end, close = function() end} end
    local buffer = {}
    return {write = function(_, value) buffer[#buffer + 1] = value end, flush = function() end,
        close = function() files[path] = table.concat(buffer) end}
end}
local map_query = assert(loadfile(map_query_source, 't', env))()
env.require = function(name)
    if name == 'object_refs' then return helpers end
    if name == 'map_query' then return map_query end
    error('unexpected require ' .. name)
end

local sampler = assert(loadfile(source, 't', env))()
local function run(from, to) for i = from, to do sampler.sample(i, pawn, 'EndGame', 'sample-time') end end

run(1, 6)
assert(files[root .. 'route-sweep.json'] == nil, 'idle sweep wrote a response')
assert(sweeps == 0, 'idle sweep traced')
files[root .. 'route-sweep.request'] = 'one|200|34|88|45\n0,0,10\n1000,0,10\n1000,500,10\n'
run(7, 9)
local response = assert(files[root .. 'route-sweep.json'], 'request was not answered')
assert(response:find('"request_id":"one"', 1, true))
assert(response:find('"status":"ok"', 1, true))
-- Segment 0: the chair, then (swept again past it) the wagon.
local chair = response:find('BP_Prop_C Chair', 1, true)
local wagon = response:find('BP_Prop_C Wagon', 1, true)
assert(chair and wagon and chair < wagon, 'blockers not listed in order')
assert(response:find('"simulating_physics":true', 1, true), 'chair physics missing')
assert(response:find('"simulating_physics":false', 1, true), 'wagon physics missing')
assert(response:find('"component_bounds":{"extent":{"x":35', 1, true), 'component bounds missing')
assert(response:find('"ledge":true', 1, true) and response:find('"rise_cm":70', 1, true), 'ledge not reported')
assert(response:find('"get":2', 1, true), 'weak pointer access not recorded')
assert(sweeps == 30, 'expected one sweep per 50 cm step (20 + 10), got ' .. sweeps)
-- 32, plus 3 re-samples confirming the ledge is abrupt, not stairs.
assert(floor_probes == 35, 'expected a floor probe per step plus each segment start, got ' .. floor_probes)
assert(#profiles == 30 and profiles[1] == 'EndPlayer', 'sweeps did not use the pawn collision profile')
assert(response:find('"query":{"profile":"EndPlayer"}', 1, true), 'query profile not reported')

-- Stairs rise 50 cm per 50 cm step, past the step limit, yet in 25 cm
-- risers: walkable, swept riser by riser, no ledge.
local sweeps_before = sweeps
files[root .. 'route-sweep.request'] = 'stairs|200|34|88|45\n1900,-500,10\n2400,-500,260\n'
run(10, 12)
local stairs = assert(files[root .. 'route-sweep.json'], 'stairs request was not answered')
assert(stairs:find('"request_id":"stairs"', 1, true))
assert(not stairs:find('"ledge":true', 1, true), 'stairs reported as a ledge')
assert(sweeps - sweeps_before > 10, 'stairs not swept riser by riser')

files[root .. 'route-sweep.json'] = 'consumed'
run(13, 15)
assert(files[root .. 'route-sweep.json'] == 'consumed', 'answered request was re-read')
files[root .. 'route-sweep.request'] = 'two|50|34|88|45\n0,0,10\n1,0,10\n'
run(16, 18)
assert(files[root .. 'route-sweep.json'] == 'consumed', 'expired request answered')
files[root .. 'route-sweep.request'] = 'three|200|34|88|45\n0,0,10\n'
run(19, 21)
assert(files[root .. 'route-sweep.json'] == 'consumed', 'one-point route answered')
local channels = {}
local plain = kismet.CapsuleTraceSingle
kismet.CapsuleTraceSingle = function(self, world, s, e, r, h, channel, ...)
    channels[#channels + 1] = channel
    return plain(self, world, s, e, r, h, channel, ...)
end
kismet.CapsuleTraceSingleForObjects = function(self, world, s, e, r, h, objects, ...)
    channels[#channels + 1] = 'objects:' .. table.concat(objects, ',')
    return plain(self, world, s, e, r, h, 0, ...)
end
files[root .. 'route-sweep.request'] = 'four|200|34|88|45|channel:2\n1000,0,10\n1000,500,10\n'
run(22, 24)
assert(files[root .. 'route-sweep.json']:find('"query":{"channel":2}', 1, true), 'channel query not used')
files[root .. 'route-sweep.request'] = 'five|200|34|88|45|objects:0,1\n1000,0,10\n1000,500,10\n'
run(25, 27)
assert(files[root .. 'route-sweep.json']:find('"query":{"objects":[0,1]}', 1, true), 'object query not used')
assert(channels[1] == 2 and channels[#channels] == 'objects:0,1', 'queries not passed through')
files[root .. 'route-sweep.request'] = 'six|200|34|88|45|teleport\n1000,0,10\n1000,500,10\n'
run(28, 30)
assert(not files[root .. 'route-sweep.json']:find('"six"', 1, true), 'unknown query answered')

-- A capsule touches the merged tile containing the stairs. Ignoring that
-- tile on later capsule sweeps must not hide it from floor/riser probes.
hit_floor_tile = true
files[root .. 'route-sweep.request'] = 'floortile|200|34|88|45\n1900,-600,10\n2400,-600,260\n'
run(31, 33)
local tile_stairs = assert(files[root .. 'route-sweep.json'])
assert(tile_stairs:find('"request_id":"floortile"', 1, true), 'floor tile request was not answered')
assert(tile_stairs:find('"floor_missing":0', 1, true), 'capsule ignore hid the stair floor')
assert(not tile_stairs:find('"ledge":true', 1, true), 'hidden floor produced false stair ledges')
-- Live 2026-10-06 at Cosmo Canyon: the floor probe slipped between planks,
-- and the fall and climb back read as a drop and a ledge on the walkway.
hit_floor_tile = false
files[root .. 'route-sweep.request'] = 'planks|200|34|88|45\n0,800,10\n900,800,10\n'
run(34, 36)
local planks = assert(files[root .. 'route-sweep.json'])
assert(planks:find('"request_id":"planks"', 1, true), 'plank request was not answered')
assert(not planks:find('"drop":true', 1, true), 'a gap between planks reported as a drop')
assert(not planks:find('"ledge":true', 1, true), 'the boards after a gap reported as a ledge')
files[root .. 'route-sweep.request'] = 'hole|200|34|88|45\n0,1000,10\n900,1000,10\n'
run(37, 39)
local unseen = assert(files[root .. 'route-sweep.json'])
assert(unseen:find('"request_id":"hole"', 1, true), 'unseen walkway request was not answered')
assert(not unseen:find('"drop":true', 1, true) and not unseen:find('"ledge":true', 1, true),
    'a walkway the probes cannot see reported as a drop and a ledge')
-- The two ends find boards a hand's width away; the three samples between take the route's floor.
assert(unseen:find('"floor_unseen":3', 1, true), 'unseen floor samples not counted')
files[root .. 'route-sweep.request'] = 'shallow|200|34|88|45\n0,1200,10\n900,1200,10\n'
run(40, 42)
local shallow = assert(files[root .. 'route-sweep.json'])
assert(shallow:find('"request_id":"shallow"', 1, true), 'shallow request was not answered')
assert(not shallow:find('"ledge":true', 1, true), 'a rise back to the route read as a ledge')
files[root .. 'route-sweep.request'] = 'ramp|200|34|88|45\n0,1400,10\n900,1400,10\n'
run(43, 45)
local ramp = assert(files[root .. 'route-sweep.json'])
assert(ramp:find('"request_id":"ramp"', 1, true), 'ramp request was not answered')
assert(not ramp:find('"drop":true', 1, true), 'a floor 1.38 m under the route read as a drop')
print('route_sweep regression passed')
return response
