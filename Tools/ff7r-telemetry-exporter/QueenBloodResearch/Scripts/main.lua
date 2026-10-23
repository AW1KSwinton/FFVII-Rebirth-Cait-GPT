-- Read-only Queen's Blood research dump (developer use only).
--
-- While a card game is on screen, periodically writes the reflected state of a
-- fixed allow-list of card-game objects to queen_blood_research.json: the 3D
-- manager and its per-cell FieldInfos, hand/board card actors, and the
-- CardGame_Test_C board widgets (CardFrame_NN, LanePower_NN, CardFull_NN).
-- It never sends input, never executes external code, and only reads
-- primitive, name/text/string and object-reference properties. Disable it
-- with Disable-QueenBloodResearch.ps1 once the board schema is established.

local ROOT = "C:\\CaitGPT\\Telemetry\\"
local OUTPUT_PATH = ROOT .. "queen_blood_research.json"
local TEMP_PATH = ROOT .. "queen_blood_research.json.tmp"
local PERIOD_MS = 3000
local MAX_ARRAY_ITEMS = 24
local MAX_PROPERTIES = 160

-- Stop walking the class chain at engine base classes; the card-game fields
-- live on the concrete Blueprint/native card-game classes above these.
local STOP_CLASSES = {
    Object = true, Actor = true, Pawn = true, Character = true,
    EndCharacterBase = true, SkeletalMeshActor = true,
    EndSkeletalMeshActor = true, EndEnvironmentSkeletalMeshActor = true,
    UserWidget = true, EndUserWidget = true, Widget = true, Visual = true,
    PanelWidget = true, ContentWidget = true,
}
local PRIMITIVE_TYPES = {
    BoolProperty = true, IntProperty = true, Int8Property = true, Int16Property = true,
    Int64Property = true, UInt16Property = true, UInt32Property = true, UInt64Property = true,
    FloatProperty = true, DoubleProperty = true, ByteProperty = true, EnumProperty = true,
}
local STRING_TYPES = {NameProperty = true, StrProperty = true, TextProperty = true}
local OBJECT_TYPES = {
    ObjectProperty = true, ClassProperty = true, WeakObjectProperty = true,
    LazyObjectProperty = true, SoftObjectProperty = true,
}

local function safe(default, callback)
    local ok, value = pcall(callback)
    if ok then return value end
    return default
end

local function valid(value)
    return value ~= nil and safe(false, function() return value:IsValid() end)
end

local function path(object)
    if not valid(object) then return nil end
    local full = safe(nil, function() return object:GetFullName() end)
    if type(full) ~= "string" then return nil end
    return full
end

local function name_of(object)
    if not valid(object) then return nil end
    return safe(nil, function() return object:GetFName():ToString() end)
end

local function unwrap(value)
    local kind = safe(nil, function() return value:type() end)
    if kind == "RemoteUnrealParam" or kind == "LocalUnrealParam" then
        return safe(nil, function() return value:get() end)
    end
    return value
end

local function primitive(value)
    value = unwrap(value)
    local kind = type(value)
    if kind == "number" or kind == "boolean" or kind == "string" then return value end
    return nil
end

local function text_value(value)
    value = unwrap(value)
    if type(value) == "string" then return value end
    if value == nil then return nil end
    return safe(nil, function() return value:ToString() end)
end

local function object_value(value)
    value = unwrap(value)
    if value == nil then return nil end
    if not valid(value) then return false end
    return path(value)
end

local function array_value(value, element_type)
    local items = {}
    local count = safe(nil, function() return value:GetArrayNum() end)
    safe(nil, function()
        value:ForEach(function(_, element)
            if #items >= MAX_ARRAY_ITEMS then return end
            local item = safe(nil, function() return element:get() end)
            if OBJECT_TYPES[element_type] then
                items[#items + 1] = object_value(item) or false
            elseif STRING_TYPES[element_type] then
                items[#items + 1] = text_value(item) or false
            elseif PRIMITIVE_TYPES[element_type] then
                local number = primitive(item)
                if number == nil then number = false end
                items[#items + 1] = number
            else
                items[#items + 1] = "<" .. tostring(element_type) .. ">"
            end
        end)
    end)
    return {count = count, items = items}
end

local function property_type(property)
    return safe("?", function() return property:GetClass():GetFName():ToString() end)
end

local function array_inner_type(property)
    return safe(nil, function() return property:GetInner():GetClass():GetFName():ToString() end)
end

-- Child object properties whose class matches one of these are dumped one
-- level deeper (card faces, number labels, billboards, text).
local NESTED_CLASS_PATTERNS = {
    "^CardGame_", "^EndNumberLabel", "^EndBillboard", "^EndTextBlock", "^EndImageSet",
}

local function nested_class(object)
    local class_name = safe(nil, function() return object:GetClass():GetFName():ToString() end)
    if type(class_name) ~= "string" then return nil end
    for _, pattern in ipairs(NESTED_CLASS_PATTERNS) do
        if class_name:match(pattern) then return class_name end
    end
    return nil
end

local dump

-- Only read members the object's class is known to have: a missing member
-- is a nullptr UObject, and calling a method on it makes UE4SS log a stack
-- trace per call.
local function widget_extras(object, result)
    local class_name = safe("", function() return object:GetClass():GetFName():ToString() end)
    result["$class"] = class_name
    if class_name:match("^CardGame_") or class_name:match("^End") then
        local visibility = primitive(safe(nil, function() return object.Visibility end))
        if visibility ~= nil then result["$visibility"] = visibility end
    end
    if class_name:match("^EndBillboard") then
        local asset = safe(nil, function() return object.ImagePath.AssetPathName end)
        if asset ~= nil and type(asset) ~= "string" then asset = text_value(asset) end
        result["$image_path"] = asset
    end
    if class_name:match("TextBlock$") then
        local text = unwrap(safe(nil, function() return object.Text end))
        if text ~= nil and (type(text) == "string" or valid(text) == false) then
            result["$text"] = type(text) == "string" and text or safe(nil, function() return text:ToString() end)
        end
    end
end

-- Dump the readable reflected properties of one object, walking the class
-- chain until an engine base class.
dump = function(object, depth)
    if not valid(object) then return nil end
    depth = depth or 0
    local result = {["$path"] = path(object), ["$fields"] = {}}
    widget_extras(object, result)
    local class = safe(nil, function() return object:GetClass() end)
    local guard = 0
    local count = 0
    while valid(class) and guard < 12 and count < MAX_PROPERTIES do
        guard = guard + 1
        local class_name = name_of(class) or "?"
        if STOP_CLASSES[class_name] then break end
        safe(nil, function()
            class:ForEachProperty(function(property)
                if count >= MAX_PROPERTIES then return end
                count = count + 1
                local field = name_of(property) or "?"
                local kind = property_type(property)
                local key = class_name .. "." .. field
                if PRIMITIVE_TYPES[kind] then
                    result[key] = primitive(safe(nil, function() return object[field] end))
                elseif STRING_TYPES[kind] then
                    result[key] = text_value(safe(nil, function() return object[field] end))
                elseif OBJECT_TYPES[kind] then
                    local child = unwrap(safe(nil, function() return object[field] end))
                    result[key] = object_value(child)
                    if depth < 2 and valid(child) and nested_class(child) ~= nil then
                        result[key .. "$"] = dump(child, depth + 1)
                    end
                elseif kind == "ArrayProperty" then
                    local inner = array_inner_type(property)
                    local value = safe(nil, function() return object[field] end)
                    if value ~= nil and not (type(value) == "userdata" and safe(nil, function() return value:type() end) == "UObject") then
                        result[key] = array_value(value, inner)
                    end
                    result["$fields"][#result["$fields"] + 1] = key .. ":Array<" .. tostring(inner) .. ">"
                    return
                end
                result["$fields"][#result["$fields"] + 1] = key .. ":" .. kind
            end)
        end)
        class = safe(nil, function() return class:GetSuperStruct() end)
    end
    return result
end

local function field_info(struct)
    local function flag_count(name)
        local value = safe(nil, function() return struct[name] end)
        return safe(nil, function() return value:GetArrayNum() end)
    end
    local result = {}
    for _, name in ipairs({
        "bCursorVisible", "bCursorOverrideVisible", "bCursorOverrideSelectVisible",
        "bBuffVisible", "bDebuffVisible", "bInfluenceVisible",
        "bPredictBuffVisible", "bPredictBuffWithCard", "bPredictDebuffVisible",
        "bPredictDebuffWithCard", "bPredictFadeOutVisible", "bFocusBuffDebuffVisible",
    }) do
        result[name] = primitive(safe(nil, function() return struct[name] end))
    end
    result.flag_players = flag_count("FlagPlayers")
    result.flag_opponents = flag_count("FlagOpponents")
    return result
end

local function each_array(value, callback)
    safe(nil, function()
        value:ForEach(function(index, element)
            callback(index, safe(element, function() return element:get() end))
        end)
    end)
end

local function first_valid(class_name)
    local objects = safe(nil, function() return FindAllOf(class_name) end)
    if type(objects) ~= "table" then return nil end
    for index = 1, math.min(#objects, 8) do
        local object = objects[index]
        local full = path(object)
        if full ~= nil and not full:find("Default__", 1, true) then return object end
    end
    return nil
end

local function json(value)
    local kind = type(value)
    if value == nil then return "null" end
    if kind == "boolean" then return value and "true" or "false" end
    if kind == "number" then
        if value ~= value or value == math.huge or value == -math.huge then return "null" end
        return string.format("%.6g", value)
    end
    if kind == "string" then
        return '"' .. value:gsub('[%z\1-\31\\"]', function(character)
            return string.format('\\u%04x', string.byte(character))
        end) .. '"'
    end
    if kind == "table" then
        local is_array = next(value) == nil or #value > 0
        local parts = {}
        if is_array then
            for index = 1, #value do parts[#parts + 1] = json(value[index]) end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        for key, item in pairs(value) do
            parts[#parts + 1] = json(tostring(key)) .. ":" .. json(item)
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return json("<" .. kind .. ">")
end

local function widget_children(root, root_full, prefix, count)
    local root_path = root_full and root_full:match(" (/.*)$")
    local result = {}
    for index = 1, count do
        local name = string.format("%s_%02d", prefix, index)
        local widget = safe(nil, function() return root[name] end)
        if not valid(widget) and root_path ~= nil then
            widget = safe(nil, function() return StaticFindObject(root_path .. ".WidgetTree." .. name) end)
        end
        if valid(widget) then result[name] = dump(widget) end
    end
    return result
end

local function sample()
    local test_root = first_valid("CardGame_Test_C")
    if test_root == nil then return end
    local test_full = path(test_root)
    local snapshot = {
        captured_at_utc = os.date("!%Y-%m-%dT%H:%M:%SZ"),
        test_root = dump(test_root),
        frames = widget_children(test_root, test_full, "CardFrame", 16),
        lane_power = widget_children(test_root, test_full, "LanePower", 6),
        card_full = widget_children(test_root, test_full, "CardFull", 15),
    }
    local manager = first_valid("EndCardGame3DManager")
    if manager ~= nil then
        snapshot.manager = dump(manager)
        local infos = {}
        each_array(safe(nil, function() return manager.FieldInfos end), function(index, struct)
            infos[#infos + 1] = field_info(struct)
        end)
        snapshot.field_infos = infos
        local board_actors = {}
        each_array(safe(nil, function() return manager.CardBoardActors end), function(_, actor)
            board_actors[#board_actors + 1] = dump(actor) or false
        end)
        snapshot.board_actors = board_actors
        local hand_actors = {}
        each_array(safe(nil, function() return manager.CardHandActors end), function(_, actor)
            hand_actors[#hand_actors + 1] = dump(actor) or false
        end)
        snapshot.hand_actors = hand_actors
        snapshot.placement_actor = dump(safe(nil, function() return manager.CardPlacementActor end))
    end
    local top_root = first_valid("CardGame_Top_C")
    if top_root ~= nil then
        local hud = {}
        for _, name in ipairs({
            "Result", "Win", "Round", "PassGauge", "CountDown", "Damage", "NoResultWindow",
            "PlayerInfo", "EnemyInfo", "PlayerToken_01", "PlayerToken_02", "PlayerToken_03",
            "EnemyToken_01", "EnemyToken_02", "EnemyToken_03", "PlayerBonus_01", "EnemyBonus_01",
            "ConfirmWindow", "Dialogue", "Top",
        }) do
            local widget = safe(nil, function() return top_root[name] end)
            if valid(widget) then hud[name] = dump(widget, 1) end
        end
        snapshot.hud = hud
    end
    local file = io.open(TEMP_PATH, "wb")
    if not file then return end
    file:write(json(snapshot))
    file:close()
    os.remove(OUTPUT_PATH)
    os.rename(TEMP_PATH, OUTPUT_PATH)
end

if type(LoopInGameThreadWithDelay) == "function" then
    LoopInGameThreadWithDelay(PERIOD_MS, function() pcall(sample) end)
else
    LoopAsync(PERIOD_MS, function()
        pcall(ExecuteInGameThread, function() pcall(sample) end)
        return false
    end)
end
