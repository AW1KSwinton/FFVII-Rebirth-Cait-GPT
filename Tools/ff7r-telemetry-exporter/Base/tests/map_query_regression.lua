-- Executed in Lua 5.4 with mock UE4SS objects and an in-memory filesystem:
--   lua map_query_regression.lua ..\Scripts\map_query.lua
-- Returns the last response text so a host (e.g. the agent's pytest) can
-- validate it with the Python consumer.
local source = assert(arg[1])
local files, now = {}, 100
local root = 'C:\\CaitGPT\\Telemetry\\'
local env = setmetatable({}, {__index = _G})

local function object(name, fields)
    local value = fields or {}
    value.IsValid = function() return true end
    value.GetFullName = function() return name end
    -- Reflection over the mock's own fields, like UE4SS's ForEachProperty.
    value.GetClass = function() return {
        GetFullName = function() return 'Class ' .. name:match('^(%S+)') end,
        GetFName = function() return {ToString = function() return name:match('^(%S+)') end} end,
        ForEachProperty = function(_, callback)
            for key, field in pairs(value) do
                if type(field) ~= 'function' then
                    local kind = type(field) == 'table' and 'ObjectProperty'
                        or type(field) == 'number' and 'IntProperty' or 'StrProperty'
                    callback({GetFName = function() return {ToString = function() return key end} end,
                        GetClass = function() return {GetFName = function() return {ToString = function() return kind end} end} end})
                end
            end
        end,
    } end
    return value
end
local function geometry(x, y, w, h) return {origin = {x = x, y = y}, size = {x = w, y = h}} end
-- Map-local placement: 2 px per metre, north-up, player at local (400, 300).
local player_world = {X = 1000, Y = 2000, Z = 150}
local function to_local(x, y) return 400 + (x - player_world.X) / 50, 300 + (y - player_world.Y) / 50 end
local function widget(name, x, y, extra)
    local fields = extra or {}
    fields.IsVisible = function() return true end
    fields.GetCachedGeometry = function() return geometry(100 + x - 8, 50 + y - 8, 16, 16) end
    return object(name, fields)
end
local image = object('PaperSprite /Game/Menu/Resident/Texture/U_Icon_MainStory_Goal.U_Icon_MainStory_Goal')
local icon_root = object('Image Icon', {Brush = {ResourceObject = image}, GetChildrenCount = function() return 0 end})
local qx, qy = to_local(6000, 2000)
local quest = widget('Icon_NaviMap_C /Engine/Transient.Icon_1', qx, qy, {
    WidgetTree = {RootWidget = icon_root},
    CachedDistanceTextBlock = {IsValid = function() return true end, GetText = function() return {ToString = function() return '50m' end} end},
})
local hidden = widget('Icon_NaviMap_C /Engine/Transient.Icon_2', 10, 10)
hidden.IsVisible = function() return false end
local icons = {quest, hidden}
local icon_array = {GetArrayNum = function() return #icons end,
    ForEach = function(_, f) for i, v in ipairs(icons) do f(i, {get = function() return v end}) end end}
setmetatable(icon_array, {__index = function(_, i) return icons[i] end})
local navi = object('NaviMap_C /Engine/Transient.NaviMap', {
    Size = {X = 800, Y = 600}, PixelPerCm = 0.02, IconWidgets = icon_array,
    GetCachedGeometry = function() return geometry(100, 50, 800, 600) end,
    ConvertToPosition2D = function(_, v) local x, y = to_local(v.X, v.Y); return {X = x, Y = y} end,
})
local px, py = to_local(player_world.X, player_world.Y)
local menu = object('AreaMap_C /Engine/Transient.AreaMap', {
    IsVisible = function() return true end,
    NaviMap = navi,
    NaviCursor = widget('Map_Cursor_C /Engine/Transient.Cursor', 420, 310),
    PlayerPosIndicator = widget('Map_PlayerLoca_C /Engine/Transient.Player', px, py),
})
local map_open = true
local slate = object('SlateBlueprintLibrary Default__SlateBlueprintLibrary', {
    GetLocalSize = function(_, g) return {X = g.size.x, Y = g.size.y} end,
    GetAbsoluteSize = function(_, g) return {X = g.size.x, Y = g.size.y} end,
    LocalToAbsolute = function(_, g, p) return {X = g.origin.x + p.X, Y = g.origin.y + p.Y} end,
    AbsoluteToLocal = function(_, g, p) return {X = p.X - g.origin.x, Y = p.Y - g.origin.y} end,
    LocalToViewport = function(_, _, g, p, pixel) pixel.X = g.origin.x + p.X; pixel.Y = g.origin.y + p.Y end,
})
local height = object('EndMenuNaviMapHeightData /Game/Map.Heights', {
    BlockSize = 10000, Blocks = {X = 2, Y = 2}, BasePosition = {X = 0, Y = 0, Z = 0},
})
local height_samples = {60, 60, 60, 60}
height.HeightData = setmetatable({GetArrayNum = function() return 4 end,
    ForEach = function(_, f) for i, v in ipairs(height_samples) do f(i, {get = function() return v end}) end end},
    {__index = function(_, i) return height_samples[i] end})

env.StaticFindObject = function(path)
    if path == '/Script/UMG.Default__SlateBlueprintLibrary' then return slate end
end
-- Field prompts, as live text blocks: a visible "Talk", a hidden "Open" in
-- another prompt widget, and a visible text elsewhere that must not count.
local talk_shown = true
local function text_block(path, value, shown)
    return object('EndTextBlock ' .. path, {IsVisible = function() return shown() end,
        GetText = function() return {ToString = function() return value end} end})
end
local game = '/Engine/Transient.GameEngine_1:BP_EndGameInstance.'
local text_blocks = {
    text_block(game .. 'FieldAction_Command_C_1.WidgetTree.Txt_Command', '$level_action_talk', function() return talk_shown end),
    text_block(game .. 'FieldAction_Command_C_2.WidgetTree.Txt_Command', '$level_action_door', function() return false end),
    text_block(game .. 'Shop_C_3.WidgetTree.Txt_Command', '$level_action_other', function() return true end),
}
-- One visible Save / Load row: a panel holding a selection image and a date.
local cell_id = '/Engine/Transient.MenuRenderingWorld:SaveData_Cell_C_7'
local select_image = object('Image ' .. cell_id .. '.WidgetTree.Img_Select', {
    IsVisible = function() return true end, GetRenderOpacity = function() return 0.5 end})
local date_text = object('EndTextBlock ' .. cell_id .. '.WidgetTree.Txt_Date', {
    IsVisible = function() return true end, GetRenderOpacity = function() return 1 end,
    GetText = function() return {ToString = function() return '10/05/2026' end} end})
local cell_panel = object('CanvasPanel ' .. cell_id .. '.WidgetTree.Root', {
    IsVisible = function() return true end, GetRenderOpacity = function() return 1 end,
    GetChildrenCount = function() return 2 end,
    GetChildAt = function(_, i) return ({select_image, date_text})[i + 1] end})
local save_cell = object('SaveData_Cell_C ' .. cell_id, {IsVisible = function() return true end,
    WidgetTree = {RootWidget = cell_panel},
    GetCachedGeometry = function() return geometry(500, 120, 700, 100) end})
local subtitle = object('Subtitle00_C /Engine/Transient.Subtitle00', {
    IsVisible = function() return false end, IsInViewport = function() return false end,
    Txt_Lines = 'old retained line',
})
local tutorial_errors = 0
local tutorial = object('BattleTutorial_C /Engine/Transient.BattleTutorial', {
    IsVisible = function() tutorial_errors = tutorial_errors + 1; error('unreadable visibility') end,
    IsInViewport = function() return true end,
})
local root_widgets = {subtitle, tutorial}
local dialog_id = '/Engine/Transient.MenuRenderingWorld:Com_Window_01_C_8'
local yes_button = object('Button ' .. dialog_id .. '.WidgetTree.YesFixture', {
    IsVisible = function() return true end, HasKeyboardFocus = function() return true end,
    IsHovered = function() return false end,
    OnClicked = function() error('read-only probe invoked a button') end})
local dialog_panel = object('CanvasPanel ' .. dialog_id .. '.WidgetTree.Root', {
    IsVisible = function() return true end, GetChildrenCount = function() return 1 end,
    GetChildAt = function() return yes_button end})
local save_dialog = object('Com_Window_01_C ' .. dialog_id, {
    IsVisible = function() return true end, WidgetTree = {RootWidget = dialog_panel}})
local dialog_scan_mode = 'exact_class_only'
env.FindAllOf = function(name)
    if name == 'UserWidget' then return root_widgets end
    if name == 'Com_Window_01_C' then return {save_dialog} end
    if name == 'Com_Window_C' and dialog_scan_mode == 'both_classes' then return {save_dialog} end
    if name == 'SaveData_Cell_C' then return {save_cell} end
    if name == 'EndTextBlock' then return text_blocks end
    if name == 'EndAreaMapMenu' then return map_open and {menu} or nil end
    if name == 'EndMenuNaviMapHeightData' then return {height} end
    return nil
end
-- ``locked`` is the agent holding the response open: Windows then refuses
-- both the delete and the rename over it.
local locked = false
env.os = {
    time = function() return now end,
    remove = function(path) if locked and path:sub(-14) == 'map-query.json' then return nil end files[path] = nil end,
    rename = function(from, to)
        if locked and to:sub(-14) == 'map-query.json' then return nil, 'Permission denied' end
        files[to] = files[from]; files[from] = nil; return true
    end,
}
env.io = {open = function(path, mode)
    if mode == 'rb' then
        if not files[path] then return nil end
        return {read = function() return files[path] end, close = function() end}
    end
    local buffer = {}
    return {write = function(_, value) buffer[#buffer + 1] = value end, flush = function() end,
        close = function() files[path] = table.concat(buffer) end}
end}

local player = object('PC World:PersistentLevel.Cloud', {K2_GetActorLocation = function() return player_world end})
local sampler = assert(loadfile(source, 't', env))()
local function run(from, to) for i = from, to do sampler.sample(i, player, 'EndGame', 1, 'sample-time') end end

run(1, 6)
assert(files[root .. 'map-query.json'] == nil, 'idle probe wrote a response')
files[root .. 'map-query.request'] = 'one|200|snapshot|heights'
run(7, 9)
local response = assert(files[root .. 'map-query.json'], 'request was not answered')
assert(response:find('"request_id":"one"', 1, true))
assert(response:find('"status":"ready"', 1, true))
assert(response:find('U_Icon_MainStory_Goal', 1, true))
assert(response:find('"distance_text":"50m"', 1, true))
assert(response:find('"samples":[60,60,60,60]', 1, true))
assert(response:find('"viewport_corners":[', 1, true))
files[root .. 'map-query.json'] = 'consumed'
run(10, 15)
assert(files[root .. 'map-query.json'] == 'consumed', 'answered request was re-read')
map_open = false
files[root .. 'map-query.request'] = 'two|200|snapshot|-'
run(16, 18)
assert(files[root .. 'map-query.json']:find('"status":"map_not_open"', 1, true))
files[root .. 'map-query.request'] = 'three|50|snapshot|-'
run(19, 21)
assert(not files[root .. 'map-query.json']:find('"three"', 1, true), 'expired request answered')
files[root .. 'map-query.request'] = 'four|200|delete|-'
run(22, 24)
assert(not files[root .. 'map-query.json']:find('"four"', 1, true), 'unknown operation answered')
-- A response the agent was reading is published once it lets go.
locked = true
files[root .. 'map-query.request'] = 'five|200|snapshot|-'
run(25, 27)
assert(not files[root .. 'map-query.json']:find('"five"', 1, true), 'locked response replaced')
locked = false
run(28, 28)
assert(files[root .. 'map-query.json']:find('"five"', 1, true), 'refused response never published')
-- The field prompt alone: only the visible Talk, and no all-texts scan.
local function prompt_request(id, from)
    files[root .. 'map-query.request'] = id .. '|200|snapshot|fieldprompt'
    run(from, from + 2)
    local answer = assert(files[root .. 'map-query.json'])
    assert(answer:find('"request_id":"' .. id .. '"', 1, true), id .. ' not answered')
    assert(answer:find('"texts":[]', 1, true), 'field prompt read scanned all texts')
    return answer
end
assert(prompt_request('six', 29):find('"field_prompt":{"commands":["$level_action_talk"],"source":"scan"}', 1, true))
-- Later reads check the kept blocks, and see the prompt go.
assert(prompt_request('seven', 32):find('"field_prompt":{"commands":["$level_action_talk"],"source":"kept_blocks"}', 1, true))
talk_shown = false
assert(prompt_request('eight', 35):find('"field_prompt":{"commands":[],"source":"kept_blocks"}', 1, true))
-- With nothing shown, an old scan is repeated in case of a new widget.
now = now + 11
assert(prompt_request('nine', 38):find('"field_prompt":{"commands":[],"source":"scan"}', 1, true))
-- Menu texts alone: visible EndTextBlocks, without the map-menu probes.
talk_shown = true
files[root .. 'map-query.request'] = 'ten|200|snapshot|menutexts'
run(41, 43)
local menu_response = assert(files[root .. 'map-query.json'])
assert(menu_response:find('"request_id":"ten"', 1, true), 'menu texts request not answered')
assert(menu_response:find('"texts_scope":"menu"', 1, true), menu_response)
assert(menu_response:find('$level_action_talk', 1, true) and menu_response:find('$level_action_other', 1, true), menu_response)
assert(not menu_response:find('$level_action_door', 1, true), 'a hidden text was listed')
assert(not menu_response:find('fast_travel_prompt', 1, true), 'menu texts probed the map menus')
-- Save / Load rows with their nodes and screen position.
files[root .. 'map-query.request'] = 'eleven|200|snapshot|savecells'
run(44, 46)
local cells_response = assert(files[root .. 'map-query.json'])
assert(cells_response:find('"request_id":"eleven"', 1, true), 'save cells request not answered')
assert(cells_response:find('"name":"Img_Select","opacity":0.5', 1, true) or cells_response:find('"Img_Select"', 1, true), cells_response)
assert(cells_response:find('"absolute":{"x":500,"y":120}', 1, true), cells_response)
assert(cells_response:find('"text":"10/05/2026"', 1, true), cells_response)
assert(not cells_response:find('fast_travel_prompt', 1, true), 'save cells probed the map menus')
-- Root observations are independent of retained text; unreadable fields are
-- unknown, and a failed class.method is not called again on the next request.
files[root .. 'map-query.request'] = 'twelve|200|snapshot|uiroots'
run(47, 49)
local roots_response = assert(files[root .. 'map-query.json'])
assert(roots_response:find('"class":"Subtitle00_C","configured_root":true,"in_viewport":false,"is_visible":false', 1, true), roots_response)
assert(roots_response:find('"input_authorized":false', 1, true), roots_response)
assert(not roots_response:find('old retained line', 1, true), 'root census relied on stale text')
assert(tutorial_errors == 1)
files[root .. 'map-query.request'] = 'thirteen|200|snapshot|uiroots'
run(50, 52)
assert(tutorial_errors == 1, 'failed root method was retried')
-- Live field census: visible child widgets exhausted the response before
-- the configured subtitle/tutorial roots later in the object array.
root_widgets = {}
for i = 1, 140 do
    root_widgets[#root_widgets + 1] = object('HUDChild_C /Engine/Transient.Child' .. i, {
        IsVisible = function() return true end, IsInViewport = function() return false end,
    })
end
root_widgets[#root_widgets + 1] = subtitle
root_widgets[#root_widgets + 1] = tutorial
files[root .. 'map-query.request'] = 'fourteen|200|snapshot|uiroots'
run(53, 55)
local crowded_response = assert(files[root .. 'map-query.json'])
assert(crowded_response:find('"class":"Subtitle00_C"', 1, true), 'child widgets displaced subtitle root')
assert(crowded_response:find('"class":"BattleTutorial_C"', 1, true), 'child widgets displaced tutorial root')
assert(crowded_response:find('"scanned":142', 1, true), crowded_response)
assert(crowded_response:find('"truncated":true', 1, true), 'omitted generic widgets were not reported')
local _, widget_count = crowded_response:gsub('"configured_root":', '')
assert(widget_count == 128, 'root census exceeded its response bound')
assert(tutorial_errors == 1, 'crowded census retried the failed method')
assert(cells_response:find('"save_dialogs":[', 1, true), 'save dialog evidence missing')
assert(cells_response:find('"keyboard_focus":true', 1, true), 'confirmation focus was not read')
assert(cells_response:find('YesFixture', 1, true), 'confirmation node name was not read')
assert(cells_response:find('"class":"Com_Window_01_C"', 1, true), 'known concrete confirmation class was missed')
-- If a concrete window also appears under the base scan, it remains one
-- window rather than becoming an ambiguous pair at the save guard.
dialog_scan_mode = 'both_classes'
files[root .. 'map-query.request'] = 'fifteen|200|snapshot|savecells'
run(56, 58)
local duplicate_response = assert(files[root .. 'map-query.json'])
local _, count = duplicate_response:gsub('"id":"Com_Window_01_C ', '')
assert(count == 1, 'one window was emitted twice by overlapping class scans')
-- Detailed roots preserve contradictory/retained evidence without granting
-- permission to advance a line. The viewport extension belongs to the game
-- widget; child visibility/opacity belong to its actual rendered contents.
subtitle.IsInViewportEx = function() return true end
subtitle.bWaitKey = true
subtitle.WidgetTree = {RootWidget = object('EndTextBlock /Engine/Transient.Subtitle00.WidgetTree.Txt_Lines', {
    IsVisible = function() return true end, GetRenderOpacity = function() return 0 end,
    GetVisibility = function() return 0 end,
    GetText = function() return 'old retained line' end,
})}
files[root .. 'map-query.request'] = 'sixteen|200|snapshot|uiroots,uirootdetails'
run(59, 61)
local detailed_response = assert(files[root .. 'map-query.json'])
assert(detailed_response:find('"in_viewport_ex":true', 1, true), detailed_response)
assert(detailed_response:find('"bWaitKey":true', 1, true), detailed_response)
assert(detailed_response:find('"opacity":0', 1, true), detailed_response)
assert(detailed_response:find('old retained line', 1, true), detailed_response)
assert(detailed_response:find('"input_authorized":false', 1, true), detailed_response)
-- Talk's fast reader skips generic HUD contents and returns only configured
-- protection roots. Cached text remains diagnostic even with zero opacity.
files[root .. 'map-query.request'] = 'seventeen|200|snapshot|npcdialogue'
run(62, 64)
local dialogue_response = assert(files[root .. 'map-query.json'])
assert(dialogue_response:find('"status":"npc_dialogue"', 1, true), dialogue_response)
assert(dialogue_response:find('"configured_complete":true', 1, true), dialogue_response)
assert(not dialogue_response:find('HUDChild_C', 1, true), 'fast reader scanned generic HUD contents')
assert(dialogue_response:find('"rendered":false', 1, true), dialogue_response)
assert(dialogue_response:find('"texts":[]', 1, true), dialogue_response)
-- A visible leaf in a hidden parent cannot authorize a click.
subtitle.WidgetTree.RootWidget.GetRenderOpacity = function() return 1 end
subtitle.WidgetTree.RootWidget.GetParent = function() return object('PanelWidget /Temp/HiddenParent', {
    IsVisible = function() return false end, GetRenderOpacity = function() return 1 end}) end
files[root .. 'map-query.request'] = 'eighteen|200|snapshot|npcdialogue'
run(65, 67)
assert(files[root .. 'map-query.json']:find('"rendered":false', 1, true), 'hidden parent was ignored')
print('map_query regression passed')
return response
