-- Explicit, one-shot card requests. Never run from idle sampling.
-- The controller signatures are from the installed EndGame.hpp, lines
-- 29521/29526. BlueprintCallable is required; it does not imply read-only.
local M = {}
local functions = {
    Player = "/Script/EndGame.EndNewCardDeckWindow:OnCardListPlayerCellSetupItem",
    Edit = "/Script/EndGame.EndNewCardDeckWindow:OnCardListEditCellSetupItem",
}
local last_ticket
local pending_reply

local function copy(value)
    if type(value) ~= "table" then return value end
    local result = {}
    for key, item in pairs(value) do result[key] = copy(item) end
    return result
end

local function equal(a, b)
    if type(a) ~= type(b) then return false end
    if type(a) ~= "table" then return a == b end
    for k, v in pairs(a) do if not equal(v, b[k]) then return false end end
    for k in pairs(b) do if a[k] == nil then return false end end
    return true
end

function M.parse(line, now)
    if type(line) ~= "string" or #line > 4096 then return nil end
    local fields = {}
    for field in (line:gsub("\n$", "") .. "\t"):gmatch("([^\t]*)\t") do
        fields[#fields + 1] = field
    end
    local bulk = fields[1] == "v2" and fields[5] == "read"
    if #fields ~= 10 or (fields[1] ~= "v1" and not bulk) or #fields[2] ~= 32
        or not fields[2]:match("^[0-9a-f]+$") or functions[fields[4]] == nil
        or (fields[5] ~= "0" and fields[5] ~= "1" and not bulk) then return nil end
    if bulk and (fields[6] ~= "-1" or fields[7] ~= "-1" or fields[8] ~= "-"
        or fields[9] ~= "-" or fields[10] ~= "0") then return nil end
    local expires = tonumber(fields[3])
    if not expires or expires % 1 ~= 0 or expires < now or expires > now + 10 then return nil end
    local row, index = tonumber(fields[6]), tonumber(fields[7])
    if not row or not index or row % 1 ~= 0 or index % 1 ~= 0 then return nil end
    local positions = {}
    if fields[9] ~= "-" then
        for position in (fields[9] .. "|"):gmatch("([^|]+)|") do
            local r, i = position:match("^(%d+),(%d+)$")
            r, i = tonumber(r), tonumber(i)
            if not r or not i or r > 19 or i > 10 or #positions >= 64 then return nil end
            positions[#positions + 1] = {row = r, index = i}
        end
        local encoded = {}
        for _, p in ipairs(positions) do encoded[#encoded + 1] = p.row .. "," .. p.index end
        if table.concat(encoded, "|") ~= fields[9] then return nil end
    end
    if fields[5] == "1" and (#positions == 0 or row < 0 or row > 19 or index < 0
        or index > 10 or not fields[8]:match("^card_id_%d%d%d$")) then return nil end
    local owned = tonumber(fields[10])
    if not owned or owned % 1 ~= 0 or owned < 0 or owned > 220 then return nil end
    return {ticket = fields[2], area = fields[4], execute = fields[5] == "1", read_only = bulk,
        restore_row = row, restore_index = index, original = fields[8], positions = positions,
        owned_count = owned}
end

function M.run(request, deps)
    local result = {ok = false, ticket = request.ticket, area = request.area,
        experimental = true, input_sent = false, population_calls = 0, cards = {}}
    local top = deps.top()
    if not deps.valid(top) or not deps.shown(top) then result.reason = "deck_screen_not_shown"; return result end
    local owner = deps.owner()
    if not deps.valid(owner) then result.reason = "controller_not_observed"; return result end
    result.controller = deps.path(owner)
    result.controller_address = deps.address and deps.address(owner) or nil
    if request.read_only then
        result.top_path = deps.path(top)
        result.top_address = deps.address(top)
        if not result.top_path or not result.controller or not result.controller_address
            or not result.top_path:find("/Engine/Transient.", 1, true)
            or result.controller:sub(1, #result.top_path + 1) ~= result.top_path .. "." then
            result.reason = "controller_root_unverified"; return result
        end
        result.guard = copy(deps.guard(top))
        result.ok = true; result.read_only = true; result.experimental = nil
        result.outcome = "controller_source_verified"
        return result
    end
    local fn = deps.find(functions[request.area])
    result.function_path = functions[request.area]
    if not deps.valid(fn) then result.reason = "population_function_unavailable"; return result end
    result.function_address = deps.address and deps.address(fn) or nil
    local ok_flags, flags = pcall(function() return fn:GetFunctionFlags() end)
    result.function_flags = ok_flags and flags or nil
    if not ok_flags or type(flags) ~= "number" or (flags & 0x04000000) == 0 then
        result.reason = "blueprint_callable_unverified"; return result
    end
    -- Reuse only the authored Card_View pane in this exact live root. Never
    -- use a CDO/template, enemy card, arbitrary UObject, or caller-supplied path.
    local view = deps.member(top, "Card_View")
    local widget = deps.member(view, "Card_Full")
    local root_path, widget_path = deps.path(top), deps.path(widget)
    if not widget_path or not root_path or not root_path:find("/Engine/Transient.", 1, true)
        or widget_path:sub(1, #root_path + 1) ~= root_path .. "." then
        result.reason = "reusable_live_widget_unavailable"; return result
    end
    result.widget_path = widget_path
    result.view_shown = deps.shown(view)
    result.before = deps.card(widget)
    result.guard_before = copy(deps.guard(top))
    if not request.execute then result.ok = true; result.outcome = "inspected"; return result end
    if not result.before or result.before.card_id ~= request.original then
        result.reason = "original_card_unverified"; return result
    end
    local cursor = result.guard_before.cursors[request.area]
    if not cursor or cursor.row ~= request.restore_row or cursor.index ~= request.restore_index then
        result.reason = "original_position_changed"; return result
    end
    local width = request.area == "Player" and 5 or 11
    local total = request.area == "Player" and tonumber((result.guard_before.total or ""):match("(%d+)/15"))
        or (request.owned_count > 0 and request.owned_count or nil)
    if not total or total < 1 then result.reason = "list_bounds_unverified"; return result end
    if request.restore_index >= width or request.restore_row * width + request.restore_index >= total then
        result.reason = "restore_position_out_of_bounds"; return result
    end
    for _, p in ipairs(request.positions) do
        if p.index >= width or p.row * width + p.index >= total then
            result.reason = "position_out_of_bounds"; return result
        end
    end
    local started = os.clock()
    local failure
    deps.busy(true)
    local loop_ok, loop_error = pcall(function()
    for _, p in ipairs(request.positions) do
        local ok, error_text = pcall(fn, owner, p.row, widget, p.index)
        result.population_calls = result.population_calls + 1
        if not ok then failure = "population_call_failed:" .. tostring(error_text):sub(1, 256); break end
        local card = deps.card(widget)
        if not card or not card.card_id then failure = "art_identity_unresolved"; break end
        result.cards[#result.cards + 1] = {row = p.row, index = p.index,
            card_id = card.card_id, image_id = card.image_id}
        if not equal(result.guard_before, deps.guard(top)) then failure = "ui_state_changed"; break end
    end
    end)
    if not loop_ok then failure = "population_read_failed:" .. tostring(loop_error):sub(1, 256) end
    -- Always attempt to restore, including after an exception/unresolved art.
    local restored, restore_error = pcall(fn, owner, request.restore_row, widget, request.restore_index)
    result.population_calls = result.population_calls + 1
    deps.busy(false)
    result.seconds = os.clock() - started
    local after_ok, after = pcall(deps.card, widget)
    local guard_ok, guard = pcall(deps.guard, top)
    result.after = after_ok and after or nil
    result.guard_after = guard_ok and guard or nil
    result.verified = false -- experiment results still need an independent baseline comparison
    result.restored = restored and equal(result.before, result.after)
        and equal(result.guard_before, result.guard_after)
    if not result.restored then failure = "widget_restore_unverified:" .. tostring(restore_error):sub(1, 128) end
    result.ok = failure == nil
    result.reason = failure
    result.outcome = result.ok and "populated_and_restored" or "experiment_refused"
    return result
end

function M.tick(root, deps)
    local file = io.open(root .. "queen_blood_cells.request", "rb")
    if not file then pending_reply = nil; return end
    local line = file:read(4097); file:close()
    local request = M.parse(line, os.time())
    if not request then pending_reply = nil; return end
    if request.ticket ~= last_ticket then
        last_ticket = request.ticket -- never retry a possibly mutating native call
        pending_reply = nil
        local ok, result = pcall(M.run, request, deps)
        if not ok then result = {ok = false, ticket = request.ticket, experimental = true,
            reason = "experiment_exception:" .. tostring(result):sub(1, 256)} end
        local encoded, body = pcall(deps.json, result)
        if encoded and type(body) == "string" then
            pending_reply = {ticket = request.ticket, body = body}
        end
    end
    if not pending_reply or pending_reply.ticket ~= request.ticket then return end
    -- Windows readers can temporarily block replacement. Retry publication
    -- on the next tick while this request is valid, retaining only bytes;
    -- never repeat M.run or hold a UObject while waiting for the file.
    local output = root .. "queen_blood_cells.json"
    local temp = output .. ".tmp"
    file = io.open(temp, "wb")
    if not file then return end
    local written = file:write(pending_reply.body)
    local flushed = file:flush()
    local closed = file:close()
    if not written or not flushed or not closed then return end
    os.remove(output)
    if os.rename(temp, output) then pending_reply = nil end
end

return M
