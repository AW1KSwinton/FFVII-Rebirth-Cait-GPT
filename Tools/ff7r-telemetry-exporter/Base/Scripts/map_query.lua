-- Optional, request-scoped area-map reader on the existing sampler thread.
--
-- Nothing is read from the map UI until the agent writes map-query.request.
-- Each request is answered exactly once with map-query.json; the probe then
-- idles until a new request id appears. Only the request file's existence is
-- checked between requests. No input, no widget mutation, no async callbacks.
--
-- Classes come from the reflected header dump (FF7R2UProj/Source/EndGame) and
-- DefaultEndMenu.ini: AreaMapWidget=AreaMap_C (UEndAreaMapMenu), its NaviMap
-- (UEndNaviMap: Size, PixelPerCm, IconWidgets, ConvertToPosition2D), NaviCursor,
-- PlayerPosIndicator, TargetIcon, TargetPosIndicator; PointWarpWidget=FastTravel_C.
local M = {}
local ROOT = "C:\\CaitGPT\\Telemetry\\"
local REQUEST_PATH = ROOT .. "map-query.request"
local RESPONSE_PATH = ROOT .. "map-query.json"
local RESPONSE_TMP_PATH = ROOT .. "map-query.json.tmp"
-- Every sample checks for a request (a small file read): every third added
-- up to ~0.4 s to each menu and prompt read.
local CHECK_EVERY_TICKS = 1
local MAX_ICONS = 512
local MAX_TREE_NODES = 24
local MAX_TREE_DEPTH = 4
local MAX_TEXTS = 6
local MAX_IMAGES = 6
local MAX_HEIGHT_SAMPLES = 65536
local MAX_HEIGHT_OBJECTS = 16
-- World-space calibration offsets (cm) around the player, fed through the
-- game's own ConvertToPosition2D. Two scales expose any non-linearity.
local CALIBRATION_OFFSETS = {
    {0, 0}, {10000, 0}, {0, 10000}, {-10000, 0}, {0, -10000},
    {100000, 0}, {0, 100000},
}
local ICON_PROPERTIES = {
    "IconKind", "Kind", "IconType", "MapIconKind", "IconID", "IconId",
    "MapIconID", "MapIconId", "ObjectID", "ObjectId", "LocationID",
    "LocationId", "bInsideArea", "bAlwaysShowDistance", "bHideDistance",
    "ArrowDirection",
}
local handled_request_id = nil
-- Stage breadcrumbs for the request being answered (map-query-trace.txt).
local TRACE_PATH = ROOT .. "map-query-trace.txt"
local detail_traces = 0
local function trace(stage)
    pcall(function()
        local f = io.open(TRACE_PATH, 'ab')
        if f then f:write(os.date('!%H:%M:%S') .. ' ' .. tostring(stage) .. '\n'); f:close() end
    end)
end
local ticks = 0

local ARRAY = {}
local function array(values)
    return setmetatable(values or {}, ARRAY)
end
local function safe(f, fallback)
    local ok, value = pcall(f)
    if ok and value ~= nil then return value end
    return fallback
end
local function valid(v)
    return safe(function() return v ~= nil and v:IsValid() end, false)
end
local function text(v)
    return safe(function()
        if v == nil then return "" end
        if type(v) == "string" then return v end
        if type(v) == "number" or type(v) == "boolean" then return tostring(v) end
        local converted_ok, converted = pcall(function() return v:ToString() end)
        if converted_ok and converted ~= nil then
            local string_ok, stringified = pcall(function() return tostring(converted) end)
            if string_ok then return stringified end
        end
        return tostring(v)
    end, "")
end
local function number(v)
    return type(v) == "number" and v == v and math.abs(v) < 1e12
end
local function vec2(v)
    local x = safe(function() return v.X end, nil)
    local y = safe(function() return v.Y end, nil)
    if number(x) and number(y) then return {x = x, y = y} end
    return nil
end
local function vec3(v)
    local x = safe(function() return v.X end, nil)
    local y = safe(function() return v.Y end, nil)
    local z = safe(function() return v.Z end, nil)
    if number(x) and number(y) and number(z) then return {x = x, y = y, z = z} end
    return nil
end
local function quoted(s)
    return '"' .. tostring(s):gsub('[%c\\"]', function(c)
        if c == '\\' then return '\\\\' end
        if c == '"' then return '\\"' end
        return string.format('\\u%04x', string.byte(c))
    end) .. '"'
end
local function json(v)
    local kind = type(v)
    if v == nil then return 'null' end
    if kind == 'string' then return quoted(v) end
    if kind == 'boolean' then return v and 'true' or 'false' end
    if kind == 'number' then
        if not number(v) then return 'null' end
        if v == math.floor(v) and math.abs(v) < 1e15 then return string.format('%.0f', v) end
        return string.format('%.6f', v)
    end
    if kind ~= 'table' then return 'null' end
    local values = {}
    if getmetatable(v) == ARRAY then
        for i = 1, #v do values[#values + 1] = json(v[i]) end
        return '[' .. table.concat(values, ',') .. ']'
    end
    local keys = {}
    for k, _ in pairs(v) do keys[#keys + 1] = tostring(k) end
    table.sort(keys)
    for _, k in ipairs(keys) do values[#values + 1] = quoted(k) .. ':' .. json(v[k]) end
    return '{' .. table.concat(values, ',') .. '}'
end

local function load_request()
    local f = io.open(REQUEST_PATH, 'rb')
    if not f then return nil end
    local s = f:read(513)
    f:close()
    if not s or #s > 512 then return nil end
    local fields = {}
    for x in s:gmatch('([^|]+)') do fields[#fields + 1] = x end
    if #fields ~= 4 then return nil end
    local id, expires, op, options = fields[1], tonumber(fields[2]), fields[3], fields[4]
    if not id:match('^%w+$') or #id > 64 then return nil end
    local now = os.time()
    if not expires or expires < now or expires > now + 600 then return nil end
    if op ~= 'snapshot' then return nil end
    if not options:match('^[%w,%-]+$') then return nil end
    local flags = {}
    for flag in options:gmatch('[^,]+') do flags[flag] = true end
    return {id = id, expires = expires, op = op, flags = flags}
end

-- Request-only root visibility census for #194/#201/#175. Text may linger
-- after an overlay closes, so report the widget's own flags independently.
-- These are diagnostics, not permission to dismiss any overlay.
local root_method_failed = {}
local root_details
local ROOT_NAMES = {
    Subtitle00_C = true, TalkMenu_00_C = true, TalkMenu_Center_C = true, BattleTutorial_C = true,
    BattleTips_C = true, MainMenu_Tutorial_Window_C = true, MainMenu_Tutorial_Frame_C = true,
    TalkMenu_ItemList_C = true, TalkMenu_ItemList_01_C = true, TalkMenu_MateriaList_C = true,
    Com_Window_C = true, Com_Window_01_C = true, Shop_C = true, Pause_00_C = true,
    MainMenu_Base_C = true, MainMenu_Base_Test_C = true, MainMenu_Card_Top_C = true,
    MainMenu_Card_TopNPC_C = true, CardGame_Top_C = true, CardGame_Test_C = true,
    Cinema_Skip_Btn_C = true,
}
local function ui_roots(with_details, configured_only)
    local objects = safe(function() return FindAllOf('UserWidget') end, nil)
    if type(objects) ~= 'table' then return {observed = false, reason = 'root_scan_unavailable', widgets = array()} end
    local result = {observed = true, widgets = array(), scanned = 0, truncated = false,
        configured_complete = true, input_authorized = false}
    -- Child widgets can report their own visibility while their parent is
    -- hidden. Preserve configured roots before filling the bounded response
    -- with these generic observations, regardless of object-array order.
    local configured, other = array(), array()
    for index, object in ipairs(objects) do
        if index > 8192 then result.truncated = true; result.configured_complete = false; break end
        if valid(object) then
            local name = text(safe(function() return object:GetFullName() end, ''))
            local class = text(safe(function() return object:GetClass():GetFName():ToString() end, ''))
            if not name:find('Default__', 1, true) then
                result.scanned = result.scanned + 1
                local function boolean(method)
                    local key = class .. '.' .. method
                    if root_method_failed[key] then return nil end
                    local ok, value = pcall(function() return object[method](object) end)
                    if not ok then root_method_failed[key] = true; return nil end
                    if type(value) == 'boolean' then return value end
                    return nil
                end
                -- Both are inherited UUserWidget/UWidget functions. Do not
                -- try game-specific properties whose existence is unproved.
                local visible, viewport
                if ROOT_NAMES[class] or not configured_only then
                    visible, viewport = boolean('IsVisible'), boolean('IsInViewport')
                end
                if ROOT_NAMES[class] or visible == true or viewport == true then
                    local destination = ROOT_NAMES[class] and configured or other
                    if #destination < 128 then
                        local entry = {path = name, class = class,
                            is_visible = visible, in_viewport = viewport, configured_root = ROOT_NAMES[class] == true}
                        if with_details and ROOT_NAMES[class] then entry.details = root_details(object, configured_only) end
                        destination[#destination + 1] = entry
                    else
                        result.truncated = true
                        if ROOT_NAMES[class] then result.configured_complete = false end
                    end
                end
            end
        end
    end
    for _, group in ipairs({configured, other}) do
        for _, entry in ipairs(group) do
            if #result.widgets < 128 then
                result.widgets[#result.widgets + 1] = entry
            else
                result.truncated = true
                if entry.configured_root then result.configured_complete = false end
            end
        end
    end
    return result
end

local function first_visible(class_name)
    local values = safe(function() return FindAllOf(class_name) end, nil)
    if type(values) ~= 'table' then return nil, 0 end
    local count = 0
    for _, object in ipairs(values) do
        count = count + 1
        if count > 64 then break end
        local name = safe(function() return object:GetFullName() end, '')
        if valid(object) and not name:find('Default__', 1, true) then
            local shown = safe(function() return object:IsVisible() end, false)
                or safe(function() return object:IsInViewport() end, false)
            if shown then return object, #values end
        end
    end
    return nil, #values
end

local function class_name(object)
    return text(safe(function() return object:GetClass():GetFullName() end, ''))
end

local function dtrace(stage)
    if detail_traces < 60 then detail_traces = detail_traces + 1; trace('  ' .. stage) end
end

local function geometry_of(slate, widget)
    if not valid(widget) or slate == nil then return nil end
    return safe(function() return widget:GetCachedGeometry() end, nil)
end

-- Centre of a widget, in absolute (desktop) Slate units and in the NaviMap's
-- local space. Both are evidence; the agent decides which space it trusts.
local function placement(slate, navi_geometry, widget)
    dtrace('placement: geometry')
    local geometry = geometry_of(slate, widget)
    dtrace('placement: geometry=' .. tostring(geometry))
    if geometry == nil then return nil end
    local size = vec2(safe(function() return slate:GetLocalSize(geometry) end, nil))
    dtrace('placement: size=' .. tostring(size))
    if size == nil then return nil end
    local centre = {X = size.x / 2, Y = size.y / 2}
    local absolute = vec2(safe(function() return slate:LocalToAbsolute(geometry, centre) end, nil))
    dtrace('placement: absolute=' .. tostring(absolute))
    local result = {size = size, absolute = absolute}
    if absolute ~= nil and navi_geometry ~= nil then
        result.navi_local = vec2(safe(function()
            return slate:AbsoluteToLocal(navi_geometry, {X = absolute.x, Y = absolute.y})
        end, nil))
        dtrace('placement: navi_local')
    end
    local slot = safe(function() return widget.Slot end, nil)
    if valid(slot) then
        result.slot_position = vec2(safe(function() return slot:GetPosition() end, nil))
        dtrace('placement: slot')
    end
    result.render_translation = vec2(safe(function() return widget.RenderTransform.Translation end, nil))
    dtrace('placement: done')
    return result
end

local function visible(widget)
    return safe(function() return widget:IsVisible() end, false) == true
end

-- Bounded walk of a user widget's tree for image resources and text.
-- Live 2026-09-26 an untyped walk aborted the sampler call past every pcall
-- (a native exception from calling a function or reading a struct the node's
-- class does not have), so each read is gated on the node's class: GetText
-- only on text blocks, Brush only on images, children only on panels, and a
-- nested user widget through its own WidgetTree.
local function node_kind(node)
    local full = text(safe(function() return node:GetFullName() end, '')) or ''
    local class = full:match('^(%S+)') or ''
    if class:match('TextBlock$') then return 'text', class end
    if class == 'Image' or class == 'EndImage' then return 'image', class end
    if class:match('_C$') then return 'user', class end
    if class:match('Panel$') or class:match('Overlay$') or class:match('Box$')
        or class:match('Border$') or class:match('Switcher$') then
        return 'panel', class
    end
    return 'other', class
end

local function describe_tree(widget)
    local images, texts, nodes = array(), array(), 0
    local root = safe(function() return widget.WidgetTree.RootWidget end, nil)
    local queue = {{root, 0}}
    local head = 1
    while head <= #queue and nodes < MAX_TREE_NODES do
        local node, depth = queue[head][1], queue[head][2]
        head = head + 1
        if valid(node) then
            nodes = nodes + 1
            local kind, class = node_kind(node)
            dtrace('tree node ' .. kind .. ' ' .. class)
            if kind == 'image' and #images < MAX_IMAGES then
                local resource = safe(function() return node.Brush.ResourceObject end, nil)
                if valid(resource) then
                    images[#images + 1] = text(safe(function() return resource:GetFullName() end, ''))
                end
            elseif kind == 'text' and #texts < MAX_TEXTS then
                local value = text(safe(function() return node:GetText() end, nil))
                if value ~= '' then texts[#texts + 1] = value end
            end
            if depth < MAX_TREE_DEPTH then
                if kind == 'panel' then
                    local count = safe(function() return node:GetChildrenCount() end, 0)
                    if type(count) == 'number' then
                        for i = 0, math.min(count, 16) - 1 do
                            queue[#queue + 1] = {safe(function() return node:GetChildAt(i) end, nil), depth + 1}
                        end
                    end
                elseif kind == 'user' then
                    queue[#queue + 1] = {safe(function() return node.WidgetTree.RootWidget end, nil), depth + 1}
                end
            end
        end
    end
    return images, texts
end

-- Live 2026-09-26 reading a candidate property name blindly aborted the
-- sampler call past every pcall. Properties are read only when reflection
-- shows the class has them and their type is a plain value.
local SAFE_PROPERTY_TYPES = {
    IntProperty = true, BoolProperty = true, ByteProperty = true, EnumProperty = true,
    NameProperty = true, StrProperty = true, FloatProperty = true,
}
local property_types_by_class = {}

local function reflected_property_types(widget)
    local class = safe(function() return widget:GetClass() end, nil)
    local key = class and text(safe(function() return class:GetFullName() end, '')) or ''
    local cached = property_types_by_class[key]
    if cached ~= nil then return cached end
    local types = {}
    safe(function()
        local walk = class
        local depth = 0
        while walk ~= nil and depth < 8 do
            walk:ForEachProperty(function(property)
                local name = text(safe(function() return property:GetFName():ToString() end, nil))
                local kind = text(safe(function() return property:GetClass():GetFName():ToString() end, nil))
                if name ~= nil and kind ~= nil and types[name] == nil then types[name] = kind end
            end)
            walk = safe(function() return walk:GetSuperStruct() end, nil)
            if walk ~= nil and not valid(walk) then walk = nil end
            depth = depth + 1
        end
        return true
    end, false)
    property_types_by_class[key] = types
    return types
end

local function icon_properties(widget)
    local found = {}
    local types = reflected_property_types(widget)
    for _, name in ipairs(ICON_PROPERTIES) do
        if SAFE_PROPERTY_TYPES[types[name] or ''] then
            local ok, value = pcall(function() return widget[name] end)
            if ok and value ~= nil then
                local rendered = text(value)
                if rendered ~= '' and #rendered <= 256 then found[name] = rendered end
            end
        end
    end
    return found
end

-- Focused, read-only evidence for finding the NPC dialogue gate. Retained
-- roots and subtitle text are not authorization to send input. Native reads
-- use inherited Widget functions; scalar properties must exist in reflection.
root_details = function(widget, fast)
    local result = {nodes = array()}
    local class = text(safe(function() return widget:GetClass():GetFName():ToString() end, ''))
    -- Only EndUserWidget implements the game's extended viewport function.
    -- The subtitle/wheel ancestry is also proven by the reflected headers.
    local extended = class == 'Subtitle00_C' or class == 'TalkMenu_00_C'
    local ancestor = safe(function() return widget:GetClass() end, nil)
    for _ = 1, 8 do
        if not valid(ancestor) then break end
        if text(safe(function() return ancestor:GetFName():ToString() end, '')) == 'EndUserWidget' then
            extended = true; break
        end
        ancestor = safe(function() return ancestor:GetSuperStruct() end, nil)
    end
    if extended then
        result.in_viewport_ex = safe(function() return widget:IsInViewportEx() end, nil)
    end
    -- The action reader needs attachment flags for protection roots, and
    -- contents only for the subtitle currently on screen. Avoid reflecting
    -- scalar properties or walking dormant menu/tutorial widget trees.
    if fast and (class ~= 'Subtitle00_C' or result.in_viewport_ex ~= true) then
        result.truncated = false
        return result
    end
    result.properties = {}
    result.property_types = fast and {} or reflected_property_types(widget)
    for name, kind in pairs(result.property_types) do
        if SAFE_PROPERTY_TYPES[kind] then
            local value = safe(function() return widget[name] end, nil)
            if type(value) == 'boolean' or type(value) == 'number' then result.properties[name] = value
            elseif type(value) == 'string' then result.properties[name] = value:sub(1, 256) end
        end
    end
    local queue = {{widget, 0}}
    local head = 1
    while head <= #queue and #result.nodes < 64 do
        local node, depth = queue[head][1], queue[head][2]
        head = head + 1
        if valid(node) then
            local kind, node_class = node_kind(node)
            local entry = {path = text(safe(function() return node:GetFullName() end, '')),
                class = node_class, depth = depth,
                visible = safe(function() return node:IsVisible() end, nil),
                visibility = text(safe(function() return node:GetVisibility() end, nil)),
                opacity = safe(function() return node:GetRenderOpacity() end, nil)}
            -- A visible text/image can belong to a hidden or transparent panel.
            -- Read each UWidget parent; never treat an unreadable flag as true.
            local parent = node
            entry.rendered = true
            for step = 1, 24 do
                local shown = safe(function() return parent:IsVisible() end, nil)
                local opacity = safe(function() return parent:GetRenderOpacity() end, nil)
                if type(shown) ~= 'boolean' or not number(opacity) then entry.rendered = nil; break end
                if not shown or opacity <= 0.05 then entry.rendered = false; break end
                parent = safe(function() return parent:GetParent() end, nil)
                if not valid(parent) then break end
                if step == 24 then entry.rendered = nil end
            end
            if kind == 'text' then entry.text = text(safe(function() return node:GetText() end, nil)) end
            result.nodes[#result.nodes + 1] = entry
            if depth < 8 then
                if kind == 'user' then
                    queue[#queue + 1] = {safe(function() return node.WidgetTree.RootWidget end, nil), depth + 1}
                elseif kind == 'panel' then
                    local count = safe(function() return node:GetChildrenCount() end, 0)
                    if type(count) == 'number' then
                        for i = 0, math.min(count, 32) - 1 do
                            queue[#queue + 1] = {safe(function() return node:GetChildAt(i) end, nil), depth + 1}
                        end
                    end
                end
            end
        end
    end
    result.truncated = head <= #queue
    return result
end

local function describe_widget(slate, navi_geometry, widget, with_tree)
    if not valid(widget) then return nil end
    dtrace('widget: start')
    local record = {runtime_id = text(safe(function() return widget:GetFullName() end, ''))}
    dtrace('widget: ' .. tostring(record.runtime_id):sub(1, 80))
    record.class = class_name(widget)
    record.visible = visible(widget)
    dtrace('widget: visible')
    record.placement = placement(slate, navi_geometry, widget)
    if with_tree then
        record.images, record.texts = describe_tree(widget)
        dtrace('widget: tree')
        record.properties = icon_properties(widget)
        dtrace('widget: properties')
        if reflected_property_types(widget).CachedDistanceTextBlock == 'ObjectProperty' then
            local block = safe(function() return widget.CachedDistanceTextBlock end, nil)
            if valid(block) then
                record.distance_text = text(safe(function() return block:GetText() end, nil))
            end
        end
        dtrace('widget: distance')
    end
    return record
end

-- Live 2026-09-26 indexing the IconWidgets TArray (value[i]) aborted the
-- sampler call past every pcall. Only GetArrayNum + ForEach are used, the
-- pattern the Queen's Blood probe reads arrays with live.
local function array_items(value, limit)
    local items = {}
    local count = safe(function() return value:GetArrayNum() end, nil)
    if type(count) ~= 'number' or count <= 0 then return items, count or 0 end
    safe(function()
        value:ForEach(function(_, element)
            if #items < limit then
                local unwrapped = safe(function() return element:get() end, element)
                if valid(unwrapped) then items[#items + 1] = unwrapped end
            end
        end)
        return true
    end, false)
    return items, count
end

local function calibration(navi, player_position)
    local samples = array()
    for _, offset in ipairs(CALIBRATION_OFFSETS) do
        local world = {x = player_position.x + offset[1], y = player_position.y + offset[2], z = player_position.z}
        local projected = vec2(safe(function()
            return navi:ConvertToPosition2D({X = world.x, Y = world.y, Z = world.z})
        end, nil))
        if projected ~= nil then samples[#samples + 1] = {world = world, map = projected} end
    end
    return samples
end

local function viewport_corners(slate, context, navi_geometry, size)
    -- LocalToViewport reports the game-viewport pixel for a local point. UE4SS
    -- populates out-parameters in the supplied tables; an unsupported call
    -- leaves them empty and the agent treats screen conversion as unavailable.
    local corners = array()
    for _, corner in ipairs({{0, 0}, {size.x, 0}, {0, size.y}, {size.x, size.y}}) do
        local pixel, viewport = {}, {}
        local ok = pcall(function()
            slate:LocalToViewport(context, navi_geometry, {X = corner[1], Y = corner[2]}, pixel, viewport)
        end)
        local pixel_value = ok and vec2(pixel) or nil
        if pixel_value == nil then return nil end
        corners[#corners + 1] = {local_point = {x = corner[1], y = corner[2]}, pixel = pixel_value}
    end
    return corners
end

local function height_data(include_samples)
    local values = safe(function() return FindAllOf('EndMenuNaviMapHeightData') end, nil)
    local result = array()
    if type(values) ~= 'table' then return result end
    for _, object in ipairs(values) do
        if #result >= MAX_HEIGHT_OBJECTS then break end
        local name = text(safe(function() return object:GetFullName() end, ''))
        if valid(object) and not name:find('Default__', 1, true) then
            local blocks = safe(function() return object.Blocks end, nil)
            local row = {
                id = name,
                block_size = safe(function() return object.BlockSize end, nil),
                blocks = {x = safe(function() return blocks.X end, nil), y = safe(function() return blocks.Y end, nil)},
                base_position = vec3(safe(function() return object.BasePosition end, nil)),
            }
            local heights = safe(function() return object.HeightData end, nil)
            local count = safe(function() return heights:GetArrayNum() end, nil)
            row.sample_count = count
            if include_samples and type(count) == 'number' and count > 0 and count <= MAX_HEIGHT_SAMPLES then
                local samples = array()
                -- ForEach, not heights[i]: indexing a TArray aborted live.
                safe(function()
                    heights:ForEach(function(index, element)
                        local h = element
                        if type(h) ~= 'number' then h = safe(function() return element:get() end, nil) end
                        samples[#samples + 1] = number(h) and h or false
                    end)
                    return true
                end, false)
                row.samples = samples
            end
            result[#result + 1] = row
        end
    end
    return result
end

local function fast_travel_prompt()
    local widget = first_visible('FastTravel_C')
    if widget == nil then return {visible = false} end
    local images, texts = describe_tree(widget)
    return {visible = true, runtime_id = text(safe(function() return widget:GetFullName() end, '')),
        texts = texts, images = images}
end

-- Every visible text on the map menus (area map, world map, fast-travel
-- prompt), found by object path like the deck-screen text scan: the
-- selected spot's name and the prompts are plain text blocks, while the
-- menu members that should hold them are not reachable.
local MAX_MAP_TEXTS = 120
local MAX_ALL_TEXTS = 400
local TEXT_CLASSES = {'EndTextBlock', 'TextBlock', 'EndRichTextBlock', 'RichTextBlock'}

local function is_map_path(path)
    return path:find('AreaMap', 1, true) or path:find('WorldMap', 1, true) or path:find('FastTravel', 1, true)
end

-- The `alltexts` flag drops the path filter and scans every text class, to
-- find where a text lives when it is not under the map menus.
local function map_texts(all, classes)
    local texts, seen = array(), {}
    local limit = all and MAX_ALL_TEXTS or MAX_MAP_TEXTS
    classes = classes or (all and TEXT_CLASSES or {'EndTextBlock'})
    for _, class in ipairs(classes) do
        local found = safe(function() return FindAllOf(class) end, nil)
        if type(found) == 'table' then
            for _, block in ipairs(found) do
                if #texts >= limit then return texts end
                if valid(block) then
                    local full = text(safe(function() return block:GetFullName() end, '')) or ''
                    local path = full:match(' (.*)$') or full
                    if not seen[full] and (all or is_map_path(path))
                        and not path:find('Default__', 1, true) and not path:find(':WidgetTree', 1, true)
                        and visible(block) then
                        seen[full] = true
                        local value = text(safe(function() return block:GetText() end, nil))
                        if value ~= nil and value ~= '' then
                            -- The widget instance that owns the text (live
                            -- 2026-10-20: the battle command list's selected
                            -- child is a reused cell, named only this way).
                            local owner = path:match('([%w_]+_C_%d+)%.WidgetTree%.[%w_]+$')
                            texts[#texts + 1] = {text = value, path = path:gsub('_C_%d+', ''):sub(-160),
                                                 class = full:match('^(%S+)'), owner = owner}
                        end
                    end
                end
            end
        end
    end
    return texts
end

-- The field interaction prompt ("Y Talk", "Y Open") alone, for the
-- `fieldprompt` flag: the visible FieldAction_Command Txt_Command texts, the
-- same blocks the `alltexts` scan reports. That scan walks every text block
-- in the game (1.5-3.5 s a read live 2026-10-05), so the blocks it finds are
-- kept and later reads only check them. A walk of the prompt widget's tree
-- missed the live Talk prompt, so the blocks are matched by path, as before.
-- The scan is repeated when the kept blocks are gone, or when none shows a
-- prompt and the last scan is older than FIELD_PROMPT_RESCAN_SECONDS (in
-- case the game has made a new prompt widget).
local FIELD_PROMPT_PATH = 'FieldAction_Command.WidgetTree.Txt_Command'
local FIELD_PROMPT_RESCAN_SECONDS = 10
local field_prompt_blocks, field_prompt_scanned_at = nil, nil

local function scan_field_prompt_blocks()
    local blocks = {}
    local found = safe(function() return FindAllOf('EndTextBlock') end, nil)
    if type(found) ~= 'table' then return blocks end
    for _, block in ipairs(found) do
        if valid(block) then
            local full = text(safe(function() return block:GetFullName() end, '')) or ''
            local path = (full:match(' (.*)$') or full):gsub('_C_%d+', '')
            if path:sub(-#FIELD_PROMPT_PATH) == FIELD_PROMPT_PATH
                and not path:find('Default__', 1, true) and not path:find(':WidgetTree', 1, true) then
                blocks[#blocks + 1] = block
            end
        end
    end
    return blocks
end

local function read_field_prompt_blocks(blocks)
    local commands, alive = array(), false
    for _, block in ipairs(blocks) do
        if valid(block) then
            alive = true
            if visible(block) then
                local value = text(safe(function() return block:GetText() end, nil))
                if value ~= nil and value ~= '' then commands[#commands + 1] = value end
            end
        end
    end
    return commands, alive
end

local function field_prompt(now)
    if field_prompt_blocks ~= nil then
        local commands, alive = read_field_prompt_blocks(field_prompt_blocks)
        if #commands > 0 or (alive and now - field_prompt_scanned_at < FIELD_PROMPT_RESCAN_SECONDS) then
            return {commands = commands, source = 'kept_blocks'}
        end
    end
    field_prompt_blocks, field_prompt_scanned_at = scan_field_prompt_blocks(), now
    local commands = read_field_prompt_blocks(field_prompt_blocks)
    return {commands = commands, source = 'scan'}
end

-- The Save / Load list's rows, for the `savecells` flag (#153): the game
-- marks the selected row only by how it is drawn (live 2026-10-05: a blue
-- fill and a cursor arrow), not by any text. Each visible SaveData_Cell
-- reports where it is on screen and its widget tree's nodes with their
-- visibility and opacity, read with the class-gated walk used above.
local MAX_SAVE_CELLS = 24
local MAX_CELL_NODES = 128
local MAX_CELL_CHILDREN = 64
local MAX_CELL_DEPTH = 12
local MAX_WIDGET_PROPERTIES = 96

-- Every plain-valued property reflection shows on the widget's class chain
-- (bools, numbers, names, enums): the selection may be a variable on the
-- row or its list rather than anything drawn differently.
local function safe_properties(widget)
    local found, count = {}, 0
    for name, kind in pairs(reflected_property_types(widget)) do
        if count >= MAX_WIDGET_PROPERTIES then break end
        if SAFE_PROPERTY_TYPES[kind] then
            local ok, value = pcall(function() return widget[name] end)
            if ok and value ~= nil then
                local rendered = text(value)
                if rendered ~= nil and #rendered <= 128 then
                    found[name] = rendered
                    count = count + 1
                end
            end
        end
    end
    return found
end

-- Primary EndUserWidget declares this BlueprintPure getter. No focus setters.
local function focused_child_id(widget)
    local cls = safe(function() return StaticFindObject('/Script/EndGame.EndUserWidget') end, nil)
    if not valid(cls) then return nil, 'class_unavailable' end
    local ok, is_user = pcall(function() return widget:IsA(cls) end)
    if not ok then return nil, 'class_check_failed' end
    if not is_user then return nil, 'not_end_user_widget' end
    local called, child = pcall(function() return widget:GetFocusedChildWidget() end)
    if not called then return nil, 'getter_failed' end
    if not valid(child) then return nil, 'no_valid_child' end
    local id = text(safe(function() return child:GetFullName() end, ''))
    if not id or id == '' then return nil, 'child_identity_unavailable' end
    return id, 'child_returned'
end

-- UWidget read-only getters. Any-user focus is diagnostic, not user-0 authority.
local function focus_diagnostics(widget)
    local result = {}
    local ok, value = pcall(function() return widget:HasAnyUserFocus() end)
    result.any_user_focus_status = ok and type(value) == 'boolean' and 'read' or 'getter_failed'
    if ok and type(value) == 'boolean' then result.any_user_focus = value end
    ok, value = pcall(function() return widget:HasFocusedDescendants() end)
    result.focused_descendants_status = ok and type(value) == 'boolean' and 'read' or 'getter_failed'
    if ok and type(value) == 'boolean' then result.focused_descendants = value end
    return result
end

local function cell_nodes(widget, include_root)
    local nodes, truncated = array(), false
    local root = include_root and widget or safe(function() return widget.WidgetTree.RootWidget end, nil)
    local queue, head = {{root, 0}}, 1
    while head <= #queue and #nodes < MAX_CELL_NODES do
        local node, depth, parent = queue[head][1], queue[head][2], queue[head][3]
        head = head + 1
        if valid(node) then
            local kind, class = node_kind(node)
            local full = text(safe(function() return node:GetFullName() end, '')) or ''
            local entry = {full_id = full, parent_full_id = parent, depth = depth, name = full:match('([^%.]+)$') or full, class = class, kind = kind,
                           visible = visible(node),
                           opacity = safe(function() return node:GetRenderOpacity() end, nil)}
            if kind == 'text' then entry.text = text(safe(function() return node:GetText() end, nil)) end
            if kind == 'user' then
                entry.focused_child_id, entry.focused_child_status = focused_child_id(node)
            end
            if kind == 'user' or class:find('Button', 1, true) then
                entry.focus_diagnostics = focus_diagnostics(node)
            end
            if class:find('Button', 1, true) then
                entry.keyboard_focus = safe(function() return node:HasKeyboardFocus() end, nil)
                entry.hovered = safe(function() return node:IsHovered() end, nil)
                entry.properties = safe_properties(node)
            end
            nodes[#nodes + 1] = entry
            if depth < MAX_CELL_DEPTH then
                if kind == 'panel' then
                    local count = safe(function() return node:GetChildrenCount() end, nil)
                    if type(count) == 'number' and count >= 0 and count < math.huge and count == math.floor(count) then
                        if count > MAX_CELL_CHILDREN then truncated = true end
                        for i = 0, math.min(count, MAX_CELL_CHILDREN) - 1 do
                            queue[#queue + 1] = {safe(function() return node:GetChildAt(i) end, nil), depth + 1, full}
                        end
                    else
                        truncated = true
                    end
                elseif kind == 'user' then
                    queue[#queue + 1] = {safe(function() return node.WidgetTree.RootWidget end, nil), depth + 1, full}
                end
            elseif kind == "panel" or kind == "user" then
                truncated = true
            end
        else
            truncated = true
        end
    end
    if head <= #queue then truncated = true end
    return nodes, {truncated = truncated, node_limit = MAX_CELL_NODES, depth_limit = MAX_CELL_DEPTH, visited = #nodes}
end

local function save_cells(result, read_title)
    local cells = array()
    local slate = safe(function() return StaticFindObject('/Script/UMG.Default__SlateBlueprintLibrary') end, nil)
    if not valid(slate) then slate = nil end
    local found = safe(function() return FindAllOf('SaveData_Cell_C') end, nil)
    if type(found) ~= 'table' then found = {} end
    for _, widget in ipairs(found) do
        if #cells >= MAX_SAVE_CELLS then break end
        local full = text(safe(function() return widget:GetFullName() end, '')) or ''
        if valid(widget) and full ~= '' and not full:find('Default__', 1, true) and visible(widget) then
            local nodes, walk = cell_nodes(widget)
            local cell = {id = (full:match(' (.*)$') or full):sub(-120), full_id = full,
                          nodes = nodes, node_walk = walk, properties = safe_properties(widget)}
            local geometry = geometry_of(slate, widget)
            if geometry ~= nil then
                cell.absolute = vec2(safe(function() return slate:LocalToAbsolute(geometry, {X = 0, Y = 0}) end, nil))
                cell.size = vec2(safe(function() return slate:GetLocalSize(geometry) end, nil))
            end
            cells[#cells + 1] = cell
        end
    end
    -- Exact object linkage only. SelectedIndex is never converted to slot+1;
    -- stale prototypes and unrelated lists remain separate diagnostics.
    result.save_lists = array()
    if read_title then result.title_lists = array() end
    local title_class = read_title and safe(function() return StaticFindObject('/Script/EndGame.EndStartMenu') end, nil) or nil
    local discovery = {scanned = 0, prototypes_skipped = 0, candidates = 0, matched_cells = 0,
        scan_limit = 2048, record_limit = 24, scan_truncated = false, records_truncated = false,
        source_supported = false, duplicates_skipped = 0, classes = array()}
    result.save_list_discovery = discovery
    local seen = {}
    -- Separate SDK classes; both expose these three BlueprintPure getters.
    -- FindAllOf superclass coverage does not prove either discovers the other.
    for _, discovery_class in ipairs({'EndListBox', 'EndVirtualScrollBox'}) do
        local lists = safe(function() return FindAllOf(discovery_class) end, nil)
        local class_scan = {class = discovery_class, source_supported = type(lists) == 'table', scanned = 0}
        discovery.classes[#discovery.classes + 1] = class_scan
        if type(lists) == 'table' then
            discovery.source_supported = true
        for _, list in ipairs(lists) do
            if discovery.scanned >= discovery.scan_limit then discovery.scan_truncated = true; break end
            discovery.scanned = discovery.scanned + 1
            class_scan.scanned = class_scan.scanned + 1
            local full = text(safe(function() return list:GetFullName() end, '')) or ''
            local object_path = full:match('^%S+ (.+)$') or ''
            local runtime_path = object_path:sub(1, 6) == '/Temp/'
                or object_path:sub(1, 17) == '/Engine/Transient'
                or object_path:sub(1, 16) == '/EngineTransient'
            if not runtime_path or full:find('Default__', 1, true) then
                discovery.prototypes_skipped = discovery.prototypes_skipped + 1
            elseif seen[full] then
                discovery.duplicates_skipped = discovery.duplicates_skipped + 1
            elseif valid(list) and full ~= '' then
                seen[full] = true
                discovery.candidates = discovery.candidates + 1
                local index = safe(function() return list:GetSelectedIndex(0) end, nil)
                local child = type(index) == 'number' and index >= 0 and index == math.floor(index)
                    and safe(function() return list:GetChildAt(index) end, nil) or nil
                local selected = valid(child) and text(safe(function() return child:GetFullName() end, '')) or nil
                if selected == '' then selected = nil end
                local cursor = safe(function() return list:GetCursorWidget() end, nil)
                local record = {id = full, discovery_class = discovery_class, class = text(safe(function() return list:GetClass():GetFullName() end, '')), visible = visible(list),
                    user_index = 0, selected_index = index, selected_child_id = selected,
                    cursor_id = valid(cursor) and text(safe(function() return cursor:GetFullName() end, '')) or nil,
                    properties = safe_properties(list), owners = array()}
                local owner = list
                for _ = 1, 6 do
                    owner = safe(function() return owner:GetOuter() end, nil)
                    if not valid(owner) then break end
                    record.owners[#record.owners + 1] = text(safe(function() return owner:GetFullName() end, ''))
                    -- Bind the title selection to the SDK's EndStartMenu,
                    -- not a guessed Blueprint name or a fixed cursor offset.
                    if valid(title_class) and safe(function() return owner:IsA(title_class) end, false) then
                        record.title_owner_id = record.owners[#record.owners]
                        record.title_owner_visible = visible(owner)
                    end
                end
                -- EndStartMenu also owns an unselected CharaFace list. It is
                -- visible alongside List_Menu but cannot supply a menu choice.
                -- Keep it in save_lists diagnostics; only emit selected title
                -- lists. Multiple selected lists still remain ambiguous.
                if read_title and record.title_owner_id ~= nil and selected ~= nil then
                    if #result.title_lists >= discovery.record_limit then
                        discovery.records_truncated = true
                    else
                        if valid(child) then
                            record.selected_nodes, record.selected_node_walk = cell_nodes(child, true)
                        end
                        result.title_lists[#result.title_lists + 1] = record
                    end
                end
                for _, cell in ipairs(cells) do
                    if selected ~= nil and selected == cell.full_id then
                        record.selected_save_cell_id = cell.id
                        record.selected_save_cell_full_id = cell.full_id
                    end
                end
                if record.selected_save_cell_full_id ~= nil then
                    discovery.matched_cells = discovery.matched_cells + 1
                    table.insert(result.save_lists, 1, record)
                else
                    result.save_lists[#result.save_lists + 1] = record
                end
                if #result.save_lists > discovery.record_limit then
                    table.remove(result.save_lists)
                    discovery.records_truncated = true
                end
            end
        end
        end
    end
    return cells
end

-- Read-only confirmation evidence for safe saving (#215). Com_Window is
-- already identified by the menu text reader. Its Save-specific node names
-- and focus behaviour still require live capture; absence is not permission
-- to presume that Yes is selected. Never invoke a button or widget method
-- that changes state here.
local function save_dialogs()
    local dialogs, seen = array(), {}
    -- DefaultEndMenu.ini names Com_Window_01_C as MessageWindowWidget.
    -- FindAllOf includes subclasses, but its ancestry to Com_Window_C has
    -- not been proven. Scan the known concrete class too and deduplicate.
    for _, class in ipairs({'Com_Window_01_C', 'Com_Window_C'}) do
        local found = safe(function() return FindAllOf(class) end, nil)
        if type(found) == 'table' then
            for _, widget in ipairs(found) do
                if #dialogs >= 4 then break end
                local full = text(safe(function() return widget:GetFullName() end, '')) or ''
                if valid(widget) and full ~= '' and not seen[full]
                    and not full:find('Default__', 1, true) and visible(widget) then
                    seen[full] = true
                    local nodes, walk = cell_nodes(widget)
                    local focus_id, focus_status = focused_child_id(widget)
                    dialogs[#dialogs + 1] = {id = full:sub(-120), full_id = full, focused_child_id = focus_id, focused_child_status = focus_status, focus_diagnostics = focus_diagnostics(widget), class = full:match('^(%S+)'), nodes = nodes, node_walk = walk}
                end
            end
        end
    end
    return dialogs
end

local ACTORS_NEAR_RADIUS_CM = 6000
local ACTORS_NEAR_MAX = 300
local ACTORS_NEAR_SCAN_MAX = 60000
-- Every actor near the player, for the `actorsnear` flag (live 2026-10-20:
-- the Mt. Nibel mako gas clouds are neither characters nor field actions,
-- the only classes the actor scans list). One request-only walk of all
-- actors, bounded; class, identity and position only, nearest first.
local function actors_near(player_position)
    local result = {radius_cm = ACTORS_NEAR_RADIUS_CM, scanned = 0, truncated = false, actors = array()}
    if not player_position then result.reason = 'player_position_unavailable'; return result end
    local found = safe(function() return FindAllOf('Actor') end, nil)
    if type(found) ~= 'table' then result.reason = 'actor_scan_unavailable'; return result end
    local near = {}
    for _, actor in ipairs(found) do
        if result.scanned >= ACTORS_NEAR_SCAN_MAX then result.truncated = true; break end
        result.scanned = result.scanned + 1
        if valid(actor) then
            local p = vec3(safe(function() return actor:K2_GetActorLocation() end, nil))
            if p then
                local dx, dy, dz = p.x - player_position.x, p.y - player_position.y, p.z - player_position.z
                local d = math.sqrt(dx * dx + dy * dy + dz * dz)
                if d <= ACTORS_NEAR_RADIUS_CM then
                    local id = text(safe(function() return actor:GetFullName() end, '')) or ''
                    if not id:find('Default__', 1, true) then
                        near[#near + 1] = {class = class_name(actor), id = id:sub(-160), position = p,
                                           distance_cm = math.floor(d)}
                    end
                end
            end
        end
    end
    table.sort(near, function(a, b) return a.distance_cm < b.distance_cm end)
    for i, item in ipairs(near) do
        if i > ACTORS_NEAR_MAX then result.truncated = true; break end
        result.actors[#result.actors + 1] = item
    end
    return result
end

-- Every sequence-playing object, for the `census` flag: finding which one a
-- cutscene uses (live 2026-10-05). One object scan per class: for diagnosis
-- only. Each class is read only through members it has: a failed member
-- lookup, even inside pcall, leaks the Lua state's C-call budget until every
-- module fails with "C stack overflow" (live 2026-10-05).
--   player: a sequence player (Status, Sequence, IsPlaying, IsPaused)
--   actor:  a level sequence actor, read through its SequencePlayer
--   asset:  holds a LevelSequence object (camera sequence actor, wrapper)
--   media:  a media player (IsPlaying, IsPaused)
--   count:  counted only
local CENSUS_CLASSES = {
    {'LevelSequencePlayer', 'player'}, {'EndCinemaSequencePlayer', 'player'},
    {'EndDialogueSequencePlayer', 'player'}, {'EndMiniGameSequencePlayer', 'player'},
    {'LevelSequenceActor', 'actor'}, {'EndCinemaSequenceActor', 'actor'},
    {'EndCinemaMovieSequenceActor', 'actor'}, {'EndDialogueSequenceActor', 'actor'},
    {'EndMiniGameSequenceActor', 'actor'}, {'EndBattleLevelSequenceActor', 'actor'},
    {'EndCameraSequenceActor', 'asset'}, {'EndSequencerWrapperEvent', 'asset'},
    {'EndSequencerWrapperLive', 'asset'}, {'EndSequencerWrapperMovie', 'asset'},
    {'EndSequencerWrapperMovieMonitor', 'asset'}, {'MediaPlayer', 'media'},
    {'CineCameraActor', 'count'},
}
local CENSUS_PER_CLASS = 6

local function object_name(object)
    if not valid(object) then return nil end
    return text(safe(function() return object:GetFName():ToString() end, ''))
end

local function read_bool(f)
    local ok, value = pcall(f)
    if ok and type(value) == 'boolean' then return value end
    return nil
end

local function player_state(player)
    local status = safe(function() return player.Status end, nil)
    return {
        playing = read_bool(function() return player:IsPlaying() end),
        paused = read_bool(function() return player:IsPaused() end),
        status = type(status) == 'number' and status or nil,
        sequence = object_name(safe(function() return player.Sequence end, nil)),
    }
end

local function census_item(object, kind)
    local item = {}
    if kind == 'player' then
        item = player_state(object)
    elseif kind == 'actor' then
        local player = safe(function() return object.SequencePlayer end, nil)
        if valid(player) then item = player_state(player) end
        item.has_player = valid(player)
    elseif kind == 'asset' then
        item.sequence = object_name(safe(function() return object.LevelSequence end, nil))
    elseif kind == 'media' then
        item.playing = read_bool(function() return object:IsPlaying() end)
        item.paused = read_bool(function() return object:IsPaused() end)
    end
    item.class = class_name(object)
    return item
end

local function sequence_census(player)
    local census = array()
    for _, entry_class in ipairs(CENSUS_CLASSES) do
        local class, kind = entry_class[1], entry_class[2]
        local objects = safe(function() return FindAllOf(class) end, nil) or {}
        local entry = {class = class, count = 0, items = array()}
        for _, object in ipairs(objects) do
            local id = text(safe(function() return object:GetFullName() end, ''))
            if valid(object) and not id:find('Default__', 1, true) then
                entry.count = entry.count + 1
                if #entry.items < CENSUS_PER_CLASS and kind ~= 'count' then
                    local item = census_item(object, kind)
                    item.id = id:sub(1, 200)
                    entry.items[#entry.items + 1] = item
                end
            end
        end
        census[#census + 1] = entry
    end
    -- What the camera views: PlayerCameraManager.ViewTarget.Target (there is
    -- no GetViewTarget UFunction).
    local controller = valid(player) and safe(function() return player.Controller end, nil) or nil
    local camera = valid(controller) and safe(function() return controller.PlayerCameraManager end, nil) or nil
    local view = valid(camera) and safe(function() return camera.ViewTarget end, nil) or nil
    local target = view and safe(function() return view.Target end, nil) or nil
    -- The game's field API, tried here only, in gameplay: called every tick
    -- from the title screen it stopped the game with "Fatal error!".
    local field = nil
    if valid(player) then
        local api = safe(function() return StaticFindObject('/Script/EndGame.Default__EndFieldAPI') end, nil)
        if valid(api) then
            field = {skip_menu = read_bool(function() return api:IsEnableSkipPauseMenu() end),
                     field_control = read_bool(function() return api:IsEnableFieldControler(false) end)}
        end
    end
    local view_target = valid(target) and {class = class_name(target),
        id = text(safe(function() return target:GetFullName() end, '')):sub(1, 200)} or nil
    return census, view_target, field
end

local function snapshot(request, player, map_id)
    local player_position = valid(player) and vec3(safe(function() return player:K2_GetActorLocation() end, nil)) or nil
    if request.flags.npcdialogue then
        return {status = 'npc_dialogue', player_position = player_position, texts = array(), ui_roots = ui_roots(true, true)}
    end
    if request.flags.uiroots then
        return {status = 'ui_roots', player_position = player_position, texts = array(), ui_roots = ui_roots(request.flags.uirootdetails)}
    end
    local only_field_prompt = request.flags.fieldprompt
    local only_menu = request.flags.menutexts or request.flags.savecells or request.flags.startmenu
    for flag in pairs(request.flags) do
        if flag ~= 'fieldprompt' then only_field_prompt = false end
        if flag ~= 'menutexts' and flag ~= 'savecells' and flag ~= 'startmenu' then only_menu = false end
    end
    if request.flags.actorsnear then
        return {status = 'map_not_open', player_position = player_position, texts = array(),
                actors_near = actors_near(player_position)}
    end
    if request.flags.census then
        local census, view_target, field = sequence_census(player)
        return {status = 'map_not_open', player_position = player_position, texts = array(),
                sequence_census = census, view_target = view_target, field_api = field}
    end
    if only_field_prompt then
        -- The prompt alone: each map-menu probe below is a full object scan.
        return {status = 'map_not_open', player_position = player_position, texts = array(),
                field_prompt = field_prompt(os.time())}
    end
    if only_menu then
        -- Every visible text of the pause menus, which are all EndTextBlocks
        -- (live 2026-10-05): one object scan where `alltexts` and the map
        -- probes make eight on the game thread, which froze the open menu
        -- for a moment at every read.
        local result = {status = 'map_not_open', player_position = player_position, texts = array()}
        if request.flags.menutexts then
            result.texts_scope = 'menu'
            result.texts = map_texts(true, {'EndTextBlock'})
        end
        if request.flags.savecells or request.flags.startmenu then
            result.save_cells = save_cells(result, request.flags.startmenu)
            result.save_dialogs = save_dialogs()
        end
        return result
    end
    local result = {
        status = 'map_not_open',
        fast_travel_prompt = fast_travel_prompt(),
        chocobo_fast_travel_menu_visible = first_visible('EndChocoboFastTravelMenu') ~= nil,
        world_map_visible = first_visible('EndWorldMapMenu') ~= nil,
    }
    result.player_position = player_position
    trace('menu')
    if request.flags.fieldprompt then
        result.field_prompt = field_prompt(os.time())
    end
    result.texts = map_texts(request.flags.alltexts)
    local menu, menu_count = first_visible('EndAreaMapMenu')
    result.area_map_instances = menu_count
    if menu == nil then return result end
    result.area_map = {runtime_id = text(safe(function() return menu:GetFullName() end, '')), class = class_name(menu)}
    local navi = safe(function() return menu.NaviMap end, nil)
    if not valid(navi) then
        result.status = 'navimap_unavailable'
        return result
    end
    if player_position == nil then
        result.status = 'player_position_unavailable'
        return result
    end
    trace('navi')
    local slate = safe(function() return StaticFindObject('/Script/UMG.Default__SlateBlueprintLibrary') end, nil)
    if not valid(slate) then slate = nil end
    local navi_geometry = geometry_of(slate, navi)
    local navi_size = slate and navi_geometry and vec2(safe(function() return slate:GetLocalSize(navi_geometry) end, nil)) or nil
    result.navi_map = {
        runtime_id = text(safe(function() return navi:GetFullName() end, '')),
        class = class_name(navi),
        size_property = vec2(safe(function() return navi.Size end, nil)),
        pixel_per_cm = safe(function() return navi.PixelPerCm end, nil),
        local_size = navi_size,
        absolute_origin = slate and navi_geometry and vec2(safe(function() return slate:LocalToAbsolute(navi_geometry, {X = 0, Y = 0}) end, nil)) or nil,
        absolute_size = slate and navi_geometry and vec2(safe(function() return slate:GetAbsoluteSize(navi_geometry) end, nil)) or nil,
    }
    if navi_size ~= nil then
        result.navi_map.viewport_corners = viewport_corners(slate, player, navi_geometry, navi_size)
    end
    trace('calibration')
    -- Live 2026-09-26 UEndNaviMap::ConvertToPosition2D (FVector -> FVector2D)
    -- aborted the whole sampler call past every Lua pcall (a native exception
    -- UE4SS cannot marshal), so the request was never answered. Opt-in only.
    if request.flags.calibrate then
        result.calibration = calibration(navi, player_position)
    else
        result.calibration = array()
        result.calibration_skipped = 'ConvertToPosition2D aborts the sampler call'
    end
    result.cursor = describe_widget(slate, navi_geometry, safe(function() return menu.NaviCursor end, nil), false)
    result.player_indicator = describe_widget(slate, navi_geometry, safe(function() return menu.PlayerPosIndicator end, nil), false)
    result.target_icon = describe_widget(slate, navi_geometry, safe(function() return menu.TargetIcon end, nil), true)
    result.target_position_indicator = describe_widget(slate, navi_geometry, safe(function() return menu.TargetPosIndicator end, nil), false)
    trace('widgets')
    local info = safe(function() return menu.InfoWindow end, nil)
    if valid(info) then
        local _, info_texts = describe_tree(info)
        result.info_window = {visible = visible(info), texts = info_texts}
    end
    trace('icons')
    local icons = array()
    local items, total = array_items(safe(function() return navi.IconWidgets end, nil), MAX_ICONS)
    for _, widget in ipairs(items) do
        local record = describe_widget(slate, navi_geometry, widget, true)
        if record ~= nil then icons[#icons + 1] = record end
    end
    result.icons = icons
    result.icon_widget_count = total
    result.icons_complete = type(total) == 'number' and total <= MAX_ICONS
    trace('heights')
    if request.flags.heights then result.height_data = height_data(true) end
    result.status = 'ready'
    return result
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
    local encoded = json(payload)
    local f = io.open(RESPONSE_TMP_PATH, 'wb')
    if not f then return false end
    local ok = pcall(function() f:write(encoded); f:flush(); f:close() end)
    if not ok then pcall(function() f:close() end); return false end
    return publish_response()
end

function M.sample(sequence, player, map_id, paused, source_timestamp)
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
    trace('request ' .. request.id)
    local ok, payload = pcall(snapshot, request, player, map_id)
    trace('snapshot ok=' .. tostring(ok) .. (ok and '' or (' ' .. tostring(payload))))
    if not ok then payload = {status = 'error', error = text(payload)} end
    payload.schema_version = 1
    payload.source = 'ue4ss_map_query'
    payload.request_id = request.id
    payload.source_sequence = sequence
    payload.timestamp_utc = source_timestamp
    payload.map_id = map_id
    payload.paused = paused
    -- Encoding runs outside the snapshot's pcall; a failure there (live
    -- 2026-09-26 with the map open) would otherwise leave the request
    -- unanswered, and the exporter's error file is only written on one tick
    -- in ten. Always answer, with the error text if need be.
    local written_ok, written = pcall(write_response, payload)
    trace('written ok=' .. tostring(written_ok) .. ' result=' .. tostring(written))
    if not written_ok or written ~= true then
        pcall(write_response, {
            schema_version = 1, source = 'ue4ss_map_query', request_id = request.id,
            source_sequence = sequence, timestamp_utc = source_timestamp, map_id = map_id,
            paused = paused, status = 'error',
            error = 'response_write_failed: ' .. tostring(written),
        })
    end
end

M.json = json
M.array = array
return M
