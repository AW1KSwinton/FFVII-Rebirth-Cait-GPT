-- Game-thread lookups only. Long-lived state contains paths, never UObjects.
-- pcall/IsValid cannot make a retained native pointer safe after level unload.
local M = {}
local controller_path = nil
local controller_world_path = nil
local last_controller_scan = nil
local CONTROLLER_SCAN_SECONDS = 2
local last_controller_diagnostic = nil

local function report_controller(diagnostic)
    if diagnostic == last_controller_diagnostic then return end
    last_controller_diagnostic = diagnostic
    print("[FF7RObjectRefs] " .. diagnostic)
end

local function valid(object)
    if object == nil then return false end
    local ok, result = pcall(function() return object:IsValid() end)
    return ok and result == true
end

local function remember(path, object)
    if path and type(FF7RRememberObjectPath) == "function" then
        pcall(function() FF7RRememberObjectPath(path, object:GetAddress()) end)
    end
    return path
end

-- Call only on a freshly obtained object in the current game-thread callback.
function M.path(object)
    if not valid(object) then return nil end
    local ok, name = pcall(function() return object:GetFullName() end)
    if not ok or type(name) ~= "string" then return nil end
    return remember(name:match("^[^%s]+ (/.*)$"), object)
end

function M.resolve(path)
    if type(path) ~= "string" or path == "" then return nil end
    if type(FF7RResolveObjectPath) == "function" then
        local ok, object, allow_search = pcall(FF7RResolveObjectPath, path)
        if not ok then return nil end
        if valid(object) then return object end
        if not allow_search then return nil end
    end
    local ok, object = pcall(StaticFindObject, path)
    if ok and valid(object) then remember(path, object); return object end
    -- Use the native full-path overload. The short-name overload walks the
    -- entire object array, which is too expensive for periodic sampling.
    if type(FindObject) == "function" then
        ok, object = pcall(FindObject, nil, nil, path, false)
        if ok and valid(object) and M.path(object) == path then return object end
    end
    return nil
end

local function local_controller(object)
    if not valid(object) then return false end
    -- Player identifies a local controller without invoking an unavailable
    -- IsLocalPlayerController binding. Do not retain the Player wrapper either.
    local ok, result = pcall(function() return object.Player:IsValid() end)
    return ok and result == true
end

function M.GetPlayerController()
    -- The world has a stable asset path, unlike generated controller names.
    -- Follow its current local player through GameplayStatics each callback.
    -- Retain only the world path: no stale native controller or world wrapper.
    local world = M.resolve(controller_world_path)
    if valid(world) then
        local gameplay = M.resolve("/Script/Engine.Default__GameplayStatics")
        if valid(gameplay) then
            local ok, current = pcall(function() return gameplay:GetPlayerController(world, 0) end)
            if ok and local_controller(current) then return current end
        end
    end
    local controller = M.resolve(controller_path)
    if local_controller(controller) then return controller end
    controller_path = nil
    local now = os.time()
    if last_controller_scan ~= nil and now - last_controller_scan < CONTROLLER_SCAN_SECONDS then return nil end
    last_controller_scan = now
    local ok, objects = pcall(FindAllOf, "PlayerController")
    if ok and (objects == nil or (type(objects) == "table" and #objects == 0)) then
        ok, objects = pcall(FindAllOf, "Controller")
    end
    if not ok or type(objects) ~= "table" or #objects > 64 then
        report_controller("controller discovery unavailable: " .. type(objects))
        return nil
    end
    local rejected = {}
    for _, object in ipairs(objects) do
        if local_controller(object) then
            controller_path = M.path(object)
            local world_ok, current_world = pcall(function() return object:GetWorld() end)
            controller_world_path = world_ok and M.path(current_world) or nil
            local pawn_ok, pawn_valid = pcall(function() return object.Pawn:IsValid() end)
            report_controller("local controller: " .. tostring(controller_path) .. ",world=" .. tostring(controller_world_path) .. ",world_resolves=" .. tostring(M.resolve(controller_world_path) ~= nil) .. ",Pawn=" .. (pawn_ok and tostring(pawn_valid) or "unavailable"))
            return object
        end
        if #rejected < 8 and valid(object) then
            local name_ok, name = pcall(function() return object:GetFullName() end)
            local player_ok, player_valid = pcall(function() return object.Player:IsValid() end)
            rejected[#rejected + 1] = (name_ok and tostring(name) or "unknown") .. ":Player=" .. (player_ok and tostring(player_valid) or "unavailable")
        end
    end
    report_controller("no local controller among " .. #objects .. ": " .. table.concat(rejected, "; "))
    return nil
end

-- Avoid UEHelpers' shared CDO cache too: resolve each function library anew.
function M.GetKismetSystemLibrary()
    return M.resolve("/Script/Engine.Default__KismetSystemLibrary")
end

function M.GetKismetMathLibrary()
    return M.resolve("/Script/Engine.Default__KismetMathLibrary")
end

return M
