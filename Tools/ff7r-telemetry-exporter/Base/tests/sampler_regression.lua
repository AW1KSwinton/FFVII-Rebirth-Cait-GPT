-- Executed with the SDK's Lua interpreter; all game/file APIs are simulated.
-- No file or game mutation is possible through this environment.
local source_path = assert(arg[1])
local files, fail_write, fail_open, writes = {}, false, false, 0
local env = setmetatable({}, {__index=_G})
env.require = function(name)
    assert(name == "object_refs")
    return {GetPlayerController=function() return nil end, resolve=function(path) return env.StaticFindObject(path) end}
end
env.StaticFindObject = function() return nil end
local menu_scans, bench_scans = 0, 0
env.FindAllOf = function(class)
    if class == "MainMenu_Base_C" then menu_scans = menu_scans + 1 end
    if class == "EndFieldActionActorBenchBreak" then bench_scans = bench_scans + 1 end
    return {}
end
env.LoopAsync = function() error("sampler must not create asynchronous work") end
env.ExecuteInGameThread = function() error("sampler must not allocate coroutines") end
env.io = {open=function(path, mode)
    assert(mode == "wb")
    if fail_open and path:match("state.sample$") then return nil end
    return {
        write=function(self, value)
            if path:match("state.sample$") then
                if fail_write then error("injected write failure") end
                writes = writes + 1
            end
            files[path] = value
            return self
        end,
        flush=function() return true end,
        close=function() return true end
    }
end}
env.os = {date=os.date, remove=function(path) files[path]=nil; return true end,
    rename=function(a,b) files[b]=files[a]; files[a]=nil; return true end}
assert(loadfile(source_path, "t", env))()
assert(type(env.FF7RReadOnlyTick) == "function")
local health_path = "C:\\CaitGPT\\Telemetry\\telemetry-health.json"
local sample_path = "C:\\CaitGPT\\Telemetry\\state.sample"
for i=1,4000 do env.FF7RReadOnlyTick() end
assert(writes == 4000, "persistent callback stopped before 4000 samples")
-- The object-array scan for the menu widget hitched the game every ten
-- samples; with no widget found it now repeats every 500 samples.
assert(menu_scans <= 10, "menu widget rescanned " .. menu_scans .. " times in 4000 samples")
-- Bench scans likewise (at most one per 15 bench checks, one per 20 samples).
assert(bench_scans <= 14, "benches rescanned " .. bench_scans .. " times in 4000 samples")
local previous = files[sample_path]
fail_write = true
env.FF7RReadOnlyTick()
assert(files[sample_path] == previous, "failed write replaced the last sample")
assert(files[health_path]:find('"state":"degraded"',1,true))
fail_write = false
env.FF7RReadOnlyTick()
assert(writes == 4001)
assert(files[health_path]:find('"consecutive_failures":0',1,true))
fail_open = true
previous = files[sample_path]
env.FF7RReadOnlyTick()
assert(files[sample_path] == previous)
assert(files[health_path]:find('state_sample_open_failed',1,true))
fail_open = false
for i=1,1000 do env.FF7RReadOnlyTick() end
assert(writes == 5001)
assert(files[health_path]:find('"state":"running"',1,true))
print("PASS: 5001 samples, no async scheduling, write/open failure recovery, health evidence")

local capsule = {
    IsValid=function() return true end, IsA=function() return true end,
    K2_GetComponentLocation=function() return {X=10,Y=20,Z=200} end,
    GetUpVector=function() return {X=0,Y=0,Z=1} end,
    GetScaledCapsuleRadius=function() return 40 end,
    GetScaledCapsuleHalfHeight=function() return 90 end
}
local movement = {IsValid=function() return true end,
    IsMovingOnGround=function() return true end, MovementMode=1}
local pawn = {IsValid=function() return true end,
    K2_GetActorLocation=function() return {X=10,Y=20,Z=200} end,
    K2_GetActorRotation=function() return {Pitch=0,Yaw=90,Roll=0} end,
    GetFullName=function() return "test_pawn" end,
    GetCapsuleComponent=function() return capsule end,
    GetVelocity=function() return {X=100,Y=200,Z=0} end,
    GetMovementComponent=function() return movement end
}
local reflected = {
    ["/Script/Engine.Actor:K2_GetRootComponent"]=function() return capsule end,
    ["/Script/Engine.SceneComponent:K2_GetComponentLocation"]=function(o) return o:K2_GetComponentLocation() end,
    ["/Script/Engine.SceneComponent:GetUpVector"]=function(o) return o:GetUpVector() end,
    ["/Script/Engine.CapsuleComponent:GetScaledCapsuleRadius"]=function(o) return o:GetScaledCapsuleRadius() end,
    ["/Script/Engine.CapsuleComponent:GetScaledCapsuleHalfHeight"]=function(o) return o:GetScaledCapsuleHalfHeight() end,
    ["/Script/Engine.Actor:GetVelocity"]=function(o) return o:GetVelocity() end,
    ["/Script/Engine.Pawn:GetMovementComponent"]=function() return movement end,
    ["/Script/Engine.NavMovementComponent:IsMovingOnGround"]=function(o) return o:IsMovingOnGround() end
}
local physics_env=setmetatable({StaticFindObject=function(path)
    if path == "/Script/Engine.CapsuleComponent" then return {IsValid=function() return true end} end
    local fn=reflected[path]
    return fn and setmetatable({IsValid=function() return true end},{__call=function(_,obj) return fn(obj) end}) or nil
end},{__index=env})
physics_env.require=function(name)
    assert(name == "object_refs")
    return {GetPlayerController=function() return {IsValid=function() return true end,Pawn=pawn} end,
            resolve=function(path) return physics_env.StaticFindObject(path) end}
end
assert(loadfile(source_path,"t",physics_env))()
physics_env.FF7RReadOnlyTick()
assert(files[sample_path]:find("|10.000000,20.000000,110.000000,40.000000,90.000000,100.000000,200.000000,0.000000,1,-1",1,true), "feet must use measured capsule center and scaled height")
movement.IsMovingOnGround=function() return false end
physics_env.FF7RReadOnlyTick()
assert(files[sample_path]:match(",0,%-1\n$"),"airborne must not be reported as grounded")
capsule.GetScaledCapsuleHalfHeight=function() error("missing reflection") end
physics_env.FF7RReadOnlyTick()
assert(files[sample_path]:match("|\n$"), "missing capsule must stay absent while base telemetry advances")
print("PASS: measured feet/velocity, airborne state, missing reflection remains absent")

reflected["/Script/Engine.Actor:K2_GetRootComponent"] = nil
physics_env.FF7RReadOnlyTick()
assert(files[sample_path]:match("|\n$"),"missing UFunction must not be called")
print("PASS: missing UFunction is gated before invocation")
