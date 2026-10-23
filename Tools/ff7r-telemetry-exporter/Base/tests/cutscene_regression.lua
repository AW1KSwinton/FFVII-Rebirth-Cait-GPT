-- Executed with the SDK's Lua interpreter; all game/file APIs are simulated.
local source_path = assert(arg[1])
local writes = {}
local scans = 0
local registry = {}

local function fname(text)
    return {ToString = function() return text end}
end

local function object(class, fields)
    local value = fields or {}
    value.valid = true
    value.IsValid = function(self) return self.valid end
    value.GetClass = function() return {GetFName = function() return fname(class) end} end
    value.GetFullName = function() return class .. " /Game/Level." .. tostring(value) end
    registry[value:GetFullName():match(" (/.*)$")] = value
    return value
end

local function player(class, sequence_name)
    local sequence = object("LevelSequence", {GetFName = function() return fname(sequence_name) end})
    local value = object(class, {Sequence = sequence, playing = false, paused = false})
    value.IsPlaying = function(self) return self.playing end
    value.IsPaused = function(self) return self.paused end
    return value
end

-- Three cinema players loaded near Chadley long before one plays (live).
local intro = player("EndCinemaSequencePlayer", "EV_COMON_2112")
local other = player("EndCinemaSequencePlayer", "EV_COMON_2110")
local sign = player("LevelSequencePlayer", "LS_Sign")
local loaded = {intro, other}

local env = setmetatable({}, {__index = _G})
env.NotifyOnNewObject = function() error("cutscene detection must not register construction notices") end
env.StaticFindObject = function(path)
    assert(path:find("/Game/Level.", 1, true), "cutscene detection must not look up game APIs")
    return registry[path]
end
env.require = function(name)
    assert(name == "object_refs")
    return assert(loadfile(assert(arg[2]), "t", env))()
end
env.FindAllOf = function(class)
    assert(class == "LevelSequencePlayer", "one scan of the base class finds the game's subclasses")
    scans = scans + 1
    return loaded
end
env.io = {open = function(path, mode)
    assert(mode == "wb" and path:match("cutscene.state$"))
    return {write = function(self, text) writes[#writes + 1] = text; return self end,
            flush = function() return true end, close = function() return true end}
end}

local cutscene = assert(loadfile(source_path, "t", env))()
local anim = object("AnimInstance", {bPlayingCutScene = false})
local camera = object("PlayerCameraManager", {ViewTarget = {Target = object("CameraActor")}})
local cloud = object("PC0000_00_Cloud_Standard_C", {
    Mesh = object("SkeletalMeshComponent", {GetAnimInstance = function() return anim end}),
    Controller = object("PlayerController", {PlayerCameraManager = camera}),
})

local n = 0
local function tick(actor) n = n + 1; return cutscene.sample(n, actor, "2026-10-05T19:00:00Z") end
local function last() return writes[#writes] end

-- In the field: one scan at start, then none for 30 s.
tick(cloud)
assert(scans == 1 and last():find('"leader":true,"tracked":2,"anim_cutscene":false,"view_target":"CameraActor","playing":[]', 1, true), last())
for _ = 1, 299 do tick(cloud) end
assert(scans == 1, "rescanned within 30 s in the field: " .. scans)
tick(cloud)
assert(scans == 2, "expected the 30 s rescan")

-- Talking to Chadley releases Cloud: scan at once, then every second for
-- five seconds, then only every 30 s.
table.insert(loaded, sign)
tick(nil)
assert(scans == 3, "expected a scan when Cloud was released")
assert(last():find('"leader":false,"tracked":3', 1, true), last())
for _ = 1, 60 do tick(nil) end
assert(scans == 8, "expected five event rescans, got " .. (scans - 3))

-- The intro plays: listed on that tick, while the others stay idle.
intro.playing = true
sign.playing = true
tick(nil)
assert(last():find('"playing":[{"kind":"cinema","player_class":"EndCinemaSequencePlayer","sequence":"EV_COMON_2112","status":1},{"kind":"level","player_class":"LevelSequencePlayer","sequence":"LS_Sign","status":1}]', 1, true), last())
-- Paused (the skip prompt): still listed, as paused.
intro.playing, intro.paused = false, true
tick(nil)
assert(last():find('"sequence":"EV_COMON_2112","status":6', 1, true), last())

-- A destroyed player is dropped without a rescan.
intro.valid = false
local before = scans
tick(nil)
assert(scans == before and last():find('"tracked":2', 1, true) and not last():find("EV_COMON_2112", 1, true), last())

-- Heartbeat: rewritten at least once a second (ten ticks) when unchanged.
local count = #writes
for _ = 1, 10 do tick(nil) end
assert(#writes == count + 1, "expected one heartbeat write in ten unchanged ticks")

-- A leader whose anim instance lacks the flag (a mount, say): the failed
-- lookup is tried once, not every tick.
local lookups = 0
local other_anim = object("OtherAnimInstance")
setmetatable(other_anim, {__index = function(_, key)
    if key == "bPlayingCutScene" then lookups = lookups + 1; error("no member bPlayingCutScene") end
end})
local mount = object("Mount_C", {
    Mesh = object("SkeletalMeshComponent", {GetAnimInstance = function() return other_anim end}),
    Controller = object("PlayerController", {PlayerCameraManager = camera}),
})
for _ = 1, 11 do tick(mount) end
assert(lookups == 1, "a missing member was looked up " .. lookups .. " times")
assert(last():find('"anim_cutscene":null', 1, true))

print("PASS: cinema players found by occasional scans, playing/paused per tick, failed members tried once")
